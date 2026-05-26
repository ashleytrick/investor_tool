"""CRM router (refactor #16 follow-up): B5 connection management +
B9 one-shot bulk import.

  GET    /crm/connection            list every CRM connected to
                                    this tenant (no api_keys leak)
  POST   /crm/connect                save a CRM api_key (encrypted
                                    with CRM_ENCRYPTION_KEY)
  DELETE /crm/connection?provider=X remove a connection
  POST   /crm/bulk-import            one-shot pull of the connected
                                    CRM's investor list into the
                                    tenant's funds + partners

All four routes are JWT-authed (require_auth) and scoped to the
caller's per-user workspace via the `_engine_and_ws()` contextvar.
The connection rows live in `crm_connections` (one row per
provider per tenant); the api_keys are Fernet-encrypted at rest
via core/crm_secrets.py.

Paths + behavior byte-identical to the pre-extraction versions
in web/api.py.
"""
from __future__ import annotations

import datetime as _dt
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select

from core.db import crm_connections, upsert
from web.deps import _engine_and_ws, require_auth


# Whitelist of provider ids the rest of the stack supports.
# Adding a new one is a CRM-client implementation + a DB column
# review; until both ship, the endpoint refuses unknown values
# with 422 so a typo doesn't become a silently-broken row.
_SUPPORTED_CRM_PROVIDERS = {"attio", "salesforce", "hubspot"}


# ---------- schemas ----------

class CRMConnectionView(BaseModel):
    """Operator-facing view of a CRM connection. Never includes the
    plaintext api_key -- only the last 4 chars (`***abcd` shape on
    the frontend) so the operator can identify which credential is
    on file without the server ever leaking the full key back."""
    provider: str
    key_suffix: str
    connected_at: str
    last_sync_at: str | None = None
    last_sync_status: str | None = None  # 'idle' | 'syncing' | 'ok' | 'error'
    last_sync_error: str | None = None


class CRMConnectBody(BaseModel):
    provider: str = Field(
        description="CRM provider id. One of: attio | salesforce | hubspot",
    )
    api_key: str = Field(
        min_length=8,
        description=(
            "Provider's API key / personal access token. Encrypted "
            "with CRM_ENCRYPTION_KEY (Fernet) at rest; the server "
            "never returns the plaintext."
        ),
    )


class CRMBulkImportBody(BaseModel):
    provider: str = Field(
        description="CRM provider to import from (attio / salesforce / hubspot)",
    )


class CRMBulkImportResult(BaseModel):
    provider: str
    imported: int
    error: str | None = None


# Local CommandResult shape -- same fields as web.api.CommandResult.
# Duplicated so this module doesn't import back into web.api and
# create a circular import. If we ever pull CommandResult into
# web/deps.py the duplication can collapse.
class _CommandResult(BaseModel):
    ok: bool
    stdout: str
    stderr: str = ""
    returncode: int = 0


# ---------- helpers ----------

def _validate_provider(provider: str) -> str:
    p = provider.strip().lower()
    if p not in _SUPPORTED_CRM_PROVIDERS:
        raise HTTPException(
            422,
            f"unsupported CRM provider {provider!r}; supported: "
            f"{sorted(_SUPPORTED_CRM_PROVIDERS)}",
        )
    return p


def _row_to_connection_view(row: Any) -> CRMConnectionView:
    return CRMConnectionView(
        provider=row.provider,
        key_suffix=row.key_suffix,
        connected_at=row.connected_at.isoformat() if row.connected_at else "",
        last_sync_at=(
            row.last_sync_at.isoformat() if row.last_sync_at else None
        ),
        last_sync_status=row.last_sync_status,
        last_sync_error=row.last_sync_error,
    )


router = APIRouter(tags=["crm"])


# ---------- endpoints ----------

@router.get(
    "/crm/connection",
    response_model=list[CRMConnectionView],
    summary="List the workspace's CRM connections (no api_keys returned)",
)
def get_crm_connections(
    _auth: None = Depends(require_auth),
) -> list[CRMConnectionView]:
    engine, _ = _engine_and_ws()
    with engine.begin() as conn:
        rows = list(conn.execute(select(crm_connections)))
    return [_row_to_connection_view(r) for r in rows]


@router.post(
    "/crm/connect",
    response_model=CRMConnectionView,
    summary="Save a CRM api_key (encrypted at rest)",
)
def crm_connect(
    body: CRMConnectBody,
    _auth: None = Depends(require_auth),
) -> CRMConnectionView:
    """Encrypts the api_key with `CRM_ENCRYPTION_KEY` and upserts
    a connection row. Re-connecting the same provider overwrites
    (rotates) the stored key.

    Returns the operator-facing view -- no plaintext key, just the
    `key_suffix` for display.

    Errors:
      - 422 unsupported provider
      - 500 CRM_ENCRYPTION_KEY env var unset / malformed
    """
    from core.crm_secrets import (
        CRMSecretsMisconfigured, encrypt_api_key, key_suffix,
    )

    provider = _validate_provider(body.provider)
    api_key = body.api_key.strip()
    if not api_key:
        raise HTTPException(422, "api_key is empty after trim")

    try:
        ciphertext = encrypt_api_key(api_key)
    except CRMSecretsMisconfigured as exc:
        raise HTTPException(500, str(exc))

    now = _dt.datetime.now(_dt.timezone.utc)
    suffix = key_suffix(api_key)

    engine, _ = _engine_and_ws()
    with engine.begin() as conn:
        upsert(
            conn, crm_connections, ["provider"],
            {
                "provider": provider,
                "encrypted_api_key": ciphertext,
                "key_suffix": suffix,
                "connected_at": now,
                # Reset sync status on re-connect -- a new key
                # means the prior sync history doesn't apply.
                "last_sync_at": None,
                "last_sync_status": "idle",
                "last_sync_error": None,
            },
        )
        row = conn.execute(
            select(crm_connections).where(
                crm_connections.c.provider == provider,
            )
        ).first()
    return _row_to_connection_view(row)


@router.delete(
    "/crm/connection",
    response_model=_CommandResult,
    summary="Remove a CRM connection",
)
def crm_disconnect(
    provider: str = Query(
        description="Provider to disconnect (attio / salesforce / hubspot)",
    ),
    _auth: None = Depends(require_auth),
) -> _CommandResult:
    p = _validate_provider(provider)
    engine, _ = _engine_and_ws()
    with engine.begin() as conn:
        res = conn.execute(
            crm_connections.delete().where(
                crm_connections.c.provider == p,
            )
        )
    if (res.rowcount or 0) == 0:
        raise HTTPException(
            404, f"no connection on file for provider={p}",
        )
    return _CommandResult(ok=True, stdout=f"disconnected {p}")


@router.post(
    "/crm/bulk-import",
    response_model=CRMBulkImportResult,
    summary=(
        "One-shot import of the connected CRM's full investor "
        "list into local funds + partners"
    ),
)
def crm_bulk_import(
    body: CRMBulkImportBody,
    _auth: None = Depends(require_auth),
) -> CRMBulkImportResult:
    """B9. Called by the wizard after the operator first connects
    a CRM (frontend prompts: "Import your existing investors?").
    Synchronous; the response carries the count. Idempotent --
    re-running is safe.
    """
    from core.config_loader import load_workspace as _load_workspace
    from core.crm_polling import bulk_import_for_workspace
    from web.deps import _ws_path

    provider = (body.provider or "").strip().lower()
    if provider not in _SUPPORTED_CRM_PROVIDERS:
        raise HTTPException(
            422,
            f"unsupported CRM provider {provider!r}; supported: "
            f"{sorted(_SUPPORTED_CRM_PROVIDERS)}",
        )

    ws = _load_workspace(_ws_path())
    results = bulk_import_for_workspace(ws)
    matching = [r for r in results if r.provider == provider]
    if not matching:
        raise HTTPException(
            404,
            f"no connection on file for provider={provider}; "
            f"POST /crm/connect first",
        )
    r = matching[0]
    return CRMBulkImportResult(
        provider=r.provider, imported=r.inserted, error=r.error,
    )
