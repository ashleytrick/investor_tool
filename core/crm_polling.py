"""B6 (CRM fast polling): cron-triggered pollers that read recent
activity + pipeline updates from connected CRMs and persist them
to `outreach_events` (event_type 'replied' / 'sent' depending on
the activity kind) + `partner_pipeline` respectively.

Layout mirrors `core/outreach_events.poll_gmail_*_for_workspace`:
  - `poll_crm_activity_for_workspace(ws)` -> PollResult
  - `poll_crm_pipeline_for_workspace(ws)` -> PollResult

Provider abstraction: each provider gets a small client class
(`AttioCRMClient`, etc.) that implements `list_activities_since`
and `list_pipeline_updates_since`. The factory below picks the
right one based on the row in `crm_connections`.

This module deliberately does NOT import the real HTTP libraries
at module load -- httpx is pulled in inside the client class
methods so tests that mock the whole client never need a network
stub.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Any, Optional, Protocol

from sqlalchemy import select

from core.crm_secrets import CRMSecretsMisconfigured, decrypt_api_key
from core.db import (
    crm_connections, get_engine, outreach_events, partner_pipeline,
    partners, upsert,
)


@dataclass(frozen=True)
class PollResult:
    workspace: str
    provider: str
    inserted: int
    error: Optional[str] = None


class _CRMClient(Protocol):
    """Provider-agnostic shape every CRM client implements."""

    def list_activities_since(
        self, after: _dt.datetime,
    ) -> list[dict]:
        ...

    def list_pipeline_updates_since(
        self, after: _dt.datetime,
    ) -> list[dict]:
        ...


# ---------- provider client: Attio ----------

class AttioCRMClient:
    """Minimal Attio v2 client for B6. Only implements the two
    poll surfaces we need today.

    Real Attio v2 docs: https://docs.attio.com/rest-api/overview
    Activity surface = `tasks` + `comments` + entry-level
    timestamps. We keep this defensive on error -- a 4xx / 5xx
    raises a `CRMPollError` the poller turns into a per-tenant
    result entry."""

    BASE = "https://api.attio.com/v2"

    def __init__(self, api_key: str):
        self._api_key = api_key

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
        }

    def list_activities_since(
        self, after: _dt.datetime,
    ) -> list[dict]:
        """Return activity dicts shaped:
            {
              "external_id": str,
              "occurred_at": datetime UTC,
              "subject":     str | None,
              "body_snippet": str | None,
              "recipient_email": str | None,
              "thread_id":   str | None,
              "kind":        "email_sent" | "email_received" | "note"
            }
        """
        try:
            import httpx
        except ImportError:
            return []
        # Attio's tasks endpoint with a created_at filter. We
        # surface notes + tasks here; the real production code may
        # need to fan out to multiple endpoints.
        url = f"{self.BASE}/tasks"
        params = {
            "filter[created_at][gte]": after.isoformat(),
            "limit": 200,
        }
        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.get(url, params=params, headers=self._headers())
        except Exception as exc:  # noqa: BLE001
            raise CRMPollError(f"attio_http_failed: {exc}") from exc
        if resp.status_code >= 400:
            raise CRMPollError(
                f"attio_http_{resp.status_code}: {resp.text[:200]}"
            )
        try:
            body = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise CRMPollError(f"attio_bad_json: {exc}") from exc
        data = body.get("data") or []
        out: list[dict] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            iid = item.get("id") or {}
            external_id = (
                iid.get("task_id") if isinstance(iid, dict)
                else str(iid or "")
            )
            occurred_iso = item.get("created_at")
            try:
                occurred_at = (
                    _dt.datetime.fromisoformat(
                        occurred_iso.replace("Z", "+00:00")
                    )
                    if occurred_iso else _dt.datetime.now(_dt.timezone.utc)
                )
            except (TypeError, ValueError, AttributeError):
                continue
            out.append({
                "external_id": str(external_id or ""),
                "occurred_at": occurred_at,
                "subject": item.get("content_plaintext") or None,
                "body_snippet": item.get("content_plaintext") or None,
                "recipient_email": None,  # Attio tasks aren't 1:1 with an email
                "thread_id": None,
                "kind": "note",
            })
        return out

    def list_pipeline_updates_since(
        self, after: _dt.datetime,
    ) -> list[dict]:
        """Return pipeline-update dicts shaped:
            {
              "partner_email": str,     # match key to local partners
              "stage":          str,    # provider's stage name
              "updated_at":     datetime UTC,
              "notes":          str | None
            }
        """
        # Attio's deals endpoint with stage + linked person. We
        # leave this returning [] for now -- the real schema needs
        # mapping the operator's deals workspace which varies per
        # tenant. Operators will need to configure mapping in a
        # future PR.
        return []


class CRMPollError(RuntimeError):
    """Raised by provider clients on any HTTP / parse failure.
    Pollers translate to PollResult.error."""


def _client_for(provider: str, api_key: str) -> _CRMClient:
    if provider == "attio":
        return AttioCRMClient(api_key)
    raise CRMPollError(
        f"no poll client implemented for provider {provider!r}"
    )


# ---------- shared infrastructure ----------

_FIRST_RUN_LOOKBACK_DAYS = 14


def _connected_providers(engine: Any) -> list[tuple[str, str]]:
    """Return [(provider, decrypted_api_key)] for every CRM the
    tenant has connected. Decryption failures (e.g. missing env
    key) skip silently -- the poll for that tenant becomes a
    no-op rather than 5xx."""
    out: list[tuple[str, str]] = []
    with engine.begin() as conn:
        rows = list(conn.execute(
            select(
                crm_connections.c.provider,
                crm_connections.c.encrypted_api_key,
            )
        ))
    for r in rows:
        try:
            plaintext = decrypt_api_key(r.encrypted_api_key)
        except (CRMSecretsMisconfigured, Exception):  # noqa: BLE001
            continue
        out.append((r.provider, plaintext))
    return out


def _stamp_sync_status(
    engine: Any, *, provider: str,
    status: str, error: Optional[str] = None,
) -> None:
    """Best-effort stamp of last_sync_at + last_sync_status. The
    polling code calls this on entry ('syncing'), success ('ok'),
    and failure ('error') so the operator UI shows progress."""
    now = _dt.datetime.now(_dt.timezone.utc)
    try:
        with engine.begin() as conn:
            row = conn.execute(
                select(crm_connections).where(
                    crm_connections.c.provider == provider,
                )
            ).first()
            if row is None:
                return
            conn.execute(
                crm_connections.update()
                .where(crm_connections.c.provider == provider)
                .values(
                    last_sync_at=now,
                    last_sync_status=status,
                    last_sync_error=error,
                )
            )
    except Exception:  # noqa: BLE001 - never fail the poll on status stamp
        pass


def _partner_id_by_email(engine: Any) -> dict[str, str]:
    """Same shape as outreach_events.partner_by_email_lookup, but
    fetched here to keep crm_polling self-contained.

    Lowercased email -> partner_id.
    """
    with engine.begin() as conn:
        rows = conn.execute(
            select(partners.c.partner_id, partners.c.email)
        )
        return {
            (r.email or "").strip().lower(): r.partner_id
            for r in rows if r.email
        }


# ---------- public: activity polling ----------

def poll_crm_activity_for_workspace(
    ws, client_factory=None,
) -> list[PollResult]:
    """Iterate every connected provider for the workspace; persist
    new activities into `outreach_events` with source set to the
    provider id ('attio' / 'salesforce' / 'hubspot').

    `client_factory` is the test seam -- defaults to `_client_for`.
    Returns one PollResult per provider so the hook caller can
    surface per-provider errors.
    """
    if client_factory is None:
        client_factory = _client_for

    ws_path_str = str(getattr(ws, "path", ws))
    engine = get_engine(f"sqlite:///{ws.path}/data/pipeline.db")
    providers = _connected_providers(engine)
    if not providers:
        return []

    results: list[PollResult] = []
    partner_by_email = _partner_id_by_email(engine)
    for provider, api_key in providers:
        _stamp_sync_status(engine, provider=provider, status="syncing")
        try:
            client = client_factory(provider, api_key)
        except Exception as exc:  # noqa: BLE001
            results.append(PollResult(
                workspace=ws_path_str, provider=provider,
                inserted=0, error=f"client_init_failed: {exc}",
            ))
            _stamp_sync_status(
                engine, provider=provider, status="error",
                error=f"client_init_failed: {exc}",
            )
            continue
        # High-water mark from outreach_events for this provider.
        from core.outreach_events import latest_event_at
        hwm = latest_event_at(
            engine, source=provider, event_type="replied",
        )
        if hwm is None:
            after_dt = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(
                days=_FIRST_RUN_LOOKBACK_DAYS,
            )
        else:
            after_dt = hwm - _dt.timedelta(seconds=1)
        try:
            activities = list(client.list_activities_since(after_dt))
        except CRMPollError as exc:
            results.append(PollResult(
                workspace=ws_path_str, provider=provider,
                inserted=0, error=str(exc),
            ))
            _stamp_sync_status(
                engine, provider=provider, status="error",
                error=str(exc),
            )
            continue
        inserted = 0
        for act in activities:
            ext_id = act.get("external_id")
            if not ext_id:
                continue
            with engine.begin() as conn:
                existing = conn.execute(
                    select(outreach_events.c.event_id).where(
                        outreach_events.c.source == provider,
                        outreach_events.c.external_id == ext_id,
                    )
                ).first()
                if existing is not None:
                    continue
                recipient = (
                    act.get("recipient_email") or ""
                ).strip().lower()
                partner_id = (
                    partner_by_email.get(recipient) if recipient else None
                )
                upsert(
                    conn, outreach_events, ["event_id"],
                    {
                        "source": provider,
                        # Attio note / task -> log as a generic
                        # 'replied' event. Future kinds (sent /
                        # bounced) can split this out.
                        "event_type": "replied",
                        "external_id": ext_id,
                        "thread_id": act.get("thread_id"),
                        "occurred_at": act["occurred_at"],
                        "recipient_email": act.get("recipient_email"),
                        "subject": act.get("subject"),
                        "body_snippet": act.get("body_snippet"),
                        "partner_id": partner_id,
                        "draft_id": None,
                        "unread": False,
                        "created_at": _dt.datetime.now(_dt.timezone.utc),
                    },
                )
                inserted += 1
        results.append(PollResult(
            workspace=ws_path_str, provider=provider,
            inserted=inserted,
        ))
        _stamp_sync_status(engine, provider=provider, status="ok")
    return results


# ---------- public: pipeline polling ----------

def poll_crm_pipeline_for_workspace(
    ws, client_factory=None,
) -> list[PollResult]:
    """Iterate every connected provider; for each pipeline update
    the CRM reports, upsert `partner_pipeline` so the local
    pipeline stage tracks the CRM."""
    if client_factory is None:
        client_factory = _client_for

    ws_path_str = str(getattr(ws, "path", ws))
    engine = get_engine(f"sqlite:///{ws.path}/data/pipeline.db")
    providers = _connected_providers(engine)
    if not providers:
        return []

    results: list[PollResult] = []
    partner_by_email = _partner_id_by_email(engine)
    for provider, api_key in providers:
        _stamp_sync_status(engine, provider=provider, status="syncing")
        try:
            client = client_factory(provider, api_key)
        except Exception as exc:  # noqa: BLE001
            results.append(PollResult(
                workspace=ws_path_str, provider=provider,
                inserted=0, error=f"client_init_failed: {exc}",
            ))
            _stamp_sync_status(
                engine, provider=provider, status="error",
                error=f"client_init_failed: {exc}",
            )
            continue
        # We don't track a high-water mark per partner_pipeline
        # row (it's an upsert table; the CRM is the source of
        # truth for stage). Pull a wide lookback every pass.
        after_dt = (
            _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=30)
        )
        try:
            updates = list(client.list_pipeline_updates_since(after_dt))
        except CRMPollError as exc:
            results.append(PollResult(
                workspace=ws_path_str, provider=provider,
                inserted=0, error=str(exc),
            ))
            _stamp_sync_status(
                engine, provider=provider, status="error",
                error=str(exc),
            )
            continue
        applied = 0
        for u in updates:
            email = (u.get("partner_email") or "").strip().lower()
            stage = (u.get("stage") or "").strip()
            partner_id = partner_by_email.get(email) if email else None
            if not partner_id or not stage:
                continue
            with engine.begin() as conn:
                upsert(
                    conn, partner_pipeline, ["partner_id"],
                    {
                        "partner_id": partner_id,
                        "stage": stage,
                        "notes": u.get("notes"),
                        "updated_at": u.get("updated_at")
                            or _dt.datetime.now(_dt.timezone.utc),
                        "updated_by": f"crm:{provider}",
                    },
                )
                applied += 1
        results.append(PollResult(
            workspace=ws_path_str, provider=provider,
            inserted=applied,
        ))
        _stamp_sync_status(engine, provider=provider, status="ok")
    return results
