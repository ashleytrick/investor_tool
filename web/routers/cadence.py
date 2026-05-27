"""Cadence router (FR-2): follow-up-sequence configuration.

  GET  /settings/cadence              read settings + ordered touches
  PUT  /settings/cadence              upsert settings + replace touches
  POST /settings/cadence/preset       {preset: standard|patient|aggressive}
  POST /settings/cadence/pause        {paused: bool}

The data lives in two new tables in the tenant's pipeline.db:
`cadence_settings` (one row, key='default') + `cadence_touches`
(positions 2..N). On first GET the endpoint seeds the Standard
preset for new tenants so the frontend always has something to
render.

This is settings only -- the actual sequence-build loop +
follow-up draft generation land in FR-3 + FR-5.
"""
from __future__ import annotations

import datetime as _dt

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from core.db import cadence_settings, cadence_touches, upsert
from web.deps import _engine_and_ws, require_auth


# ---------- enum ----------
#
# Free-form text in the DB (for future flexibility), but the
# endpoint enforces the whitelist on PUT so the frontend always
# sees a known value. 'custom' means the operator's
# custom_prompt overrides the default angle directive.
_VALID_ANGLES = {
    "new_signal",
    "specific_ask",
    "soft_check_in",
    "graceful_close",
    "custom",
}

_VALID_PRESETS = {"standard", "patient", "aggressive"}


_KEY = "default"  # one cadence row per tenant; future PR can scope per investor


# ---------- presets ----------
#
# Spec §2.3. Touch 1 is the initial outreach (lives in
# email_drafts / outreach_events); these are touches 2..N.

_PRESETS: dict[str, dict] = {
    "standard": {
        "max_touches": 4,
        "daily_mix_new_pct": 60,
        "touches": [
            {"position": 2, "gap_days": 3,  "angle": "new_signal"},
            {"position": 3, "gap_days": 7,  "angle": "specific_ask"},
            {"position": 4, "gap_days": 14, "angle": "graceful_close"},
        ],
    },
    "patient": {
        "max_touches": 5,
        "daily_mix_new_pct": 70,
        "touches": [
            {"position": 2, "gap_days": 5,  "angle": "new_signal"},
            {"position": 3, "gap_days": 14, "angle": "soft_check_in"},
            {"position": 4, "gap_days": 21, "angle": "specific_ask"},
            {"position": 5, "gap_days": 30, "angle": "graceful_close"},
        ],
    },
    "aggressive": {
        "max_touches": 4,
        "daily_mix_new_pct": 50,
        "touches": [
            {"position": 2, "gap_days": 2, "angle": "new_signal"},
            {"position": 3, "gap_days": 4, "angle": "specific_ask"},
            {"position": 4, "gap_days": 7, "angle": "graceful_close"},
        ],
    },
}


# ---------- schemas ----------

class CadenceTouchView(BaseModel):
    position: int = Field(ge=2, description="Touch number (2..N).")
    gap_days: int = Field(ge=0, le=365, description="Days after previous touch.")
    angle: str = Field(
        description=(
            "One of: new_signal | specific_ask | soft_check_in | "
            "graceful_close | custom."
        ),
    )
    custom_prompt: str | None = None


class CadenceSettingsView(BaseModel):
    enabled: bool = True
    paused: bool = False
    max_touches: int = 4
    daily_mix_new_pct: int = 60
    auto_stop_on_reply: bool = True
    auto_stop_on_pipeline_advance: bool = True
    auto_stop_on_manual_pass: bool = True
    auto_stop_on_fund_news: bool = False
    touches: list[CadenceTouchView] = Field(default_factory=list)
    updated_at: str | None = None


class CadenceSettingsBody(BaseModel):
    enabled: bool = True
    paused: bool = False
    max_touches: int = Field(default=4, ge=1, le=10)
    daily_mix_new_pct: int = Field(default=60, ge=0, le=100)
    auto_stop_on_reply: bool = True
    auto_stop_on_pipeline_advance: bool = True
    auto_stop_on_manual_pass: bool = True
    auto_stop_on_fund_news: bool = False
    touches: list[CadenceTouchView] = Field(default_factory=list)


class PresetBody(BaseModel):
    preset: str = Field(
        description="One of: standard | patient | aggressive.",
    )


class PauseBody(BaseModel):
    paused: bool


# ---------- helpers ----------

def _read_settings(conn) -> dict:
    """Read the single cadence_settings row + ordered touches.
    Returns the Standard preset defaults if no row exists yet
    (seeded lazily on first GET / first PUT)."""
    settings_row = conn.execute(
        select(cadence_settings).where(
            cadence_settings.c.key == _KEY,
        )
    ).first()
    touches_rows = list(conn.execute(
        select(cadence_touches).order_by(cadence_touches.c.position)
    ))
    if settings_row is None:
        preset = _PRESETS["standard"]
        return {
            "enabled": True,
            "paused": False,
            "max_touches": preset["max_touches"],
            "daily_mix_new_pct": preset["daily_mix_new_pct"],
            "auto_stop_on_reply": True,
            "auto_stop_on_pipeline_advance": True,
            "auto_stop_on_manual_pass": True,
            "auto_stop_on_fund_news": False,
            "touches": preset["touches"],
            "updated_at": None,
            "_seeded": True,
        }
    return {
        "enabled": bool(settings_row.enabled),
        "paused": bool(settings_row.paused),
        "max_touches": int(settings_row.max_touches or 4),
        "daily_mix_new_pct": int(settings_row.daily_mix_new_pct or 60),
        "auto_stop_on_reply": bool(settings_row.auto_stop_on_reply),
        "auto_stop_on_pipeline_advance": bool(
            settings_row.auto_stop_on_pipeline_advance
        ),
        "auto_stop_on_manual_pass": bool(settings_row.auto_stop_on_manual_pass),
        "auto_stop_on_fund_news": bool(settings_row.auto_stop_on_fund_news),
        "touches": [
            {
                "position": int(r.position),
                "gap_days": int(r.gap_days),
                "angle": r.angle,
                "custom_prompt": r.custom_prompt,
            }
            for r in touches_rows
        ],
        "updated_at": (
            settings_row.updated_at.isoformat()
            if settings_row.updated_at else None
        ),
        "_seeded": False,
    }


def _write_settings(conn, *, settings: dict, touches: list[dict]) -> None:
    """Replace-based: wipes cadence_touches then re-inserts. Same
    semantics as the spec's `PUT /settings/cadence` ("upsert
    settings + replace touches[]")."""
    now = _dt.datetime.now(_dt.timezone.utc)
    upsert(
        conn, cadence_settings, ["key"],
        {
            "key": _KEY,
            "enabled": settings["enabled"],
            "paused": settings["paused"],
            "max_touches": settings["max_touches"],
            "daily_mix_new_pct": settings["daily_mix_new_pct"],
            "auto_stop_on_reply": settings["auto_stop_on_reply"],
            "auto_stop_on_pipeline_advance": settings[
                "auto_stop_on_pipeline_advance"
            ],
            "auto_stop_on_manual_pass": settings["auto_stop_on_manual_pass"],
            "auto_stop_on_fund_news": settings["auto_stop_on_fund_news"],
            "updated_at": now,
        },
    )
    conn.execute(cadence_touches.delete())
    for t in touches:
        conn.execute(cadence_touches.insert().values(
            position=int(t["position"]),
            gap_days=int(t["gap_days"]),
            angle=t["angle"],
            custom_prompt=t.get("custom_prompt"),
            updated_at=now,
        ))


def _validate_touches(touches: list[dict], max_touches: int) -> None:
    """422 if touches are malformed. The frontend renders the
    editor so most invalid states are caught client-side; this
    is defence-in-depth + the contract for direct API callers."""
    seen_positions: set[int] = set()
    for t in touches:
        if t["position"] < 2:
            raise HTTPException(
                422,
                f"touch position must be >= 2 (1 is the initial "
                f"outreach); got {t['position']}",
            )
        if t["position"] > max_touches:
            raise HTTPException(
                422,
                f"touch position {t['position']} exceeds "
                f"max_touches={max_touches}",
            )
        if t["angle"] not in _VALID_ANGLES:
            raise HTTPException(
                422,
                f"angle must be one of {sorted(_VALID_ANGLES)}; "
                f"got {t['angle']!r}",
            )
        if t["position"] in seen_positions:
            raise HTTPException(
                422,
                f"duplicate touch position {t['position']}",
            )
        seen_positions.add(t["position"])


router = APIRouter(tags=["cadence"])


@router.get(
    "/settings/cadence",
    response_model=CadenceSettingsView,
    summary=(
        "Read the cadence settings + ordered touches "
        "(Standard preset on first read)"
    ),
)
def get_cadence(
    _auth: None = Depends(require_auth),
) -> CadenceSettingsView:
    engine, _ = _engine_and_ws()
    with engine.begin() as conn:
        data = _read_settings(conn)
    return CadenceSettingsView(**{
        k: v for k, v in data.items() if not k.startswith("_")
    })


@router.put(
    "/settings/cadence",
    response_model=CadenceSettingsView,
    summary="Replace cadence settings + touches in one write",
)
def put_cadence(
    body: CadenceSettingsBody,
    _auth: None = Depends(require_auth),
) -> CadenceSettingsView:
    touches_payload = [t.model_dump() for t in body.touches]
    _validate_touches(touches_payload, body.max_touches)

    settings_payload = body.model_dump()
    settings_payload.pop("touches", None)

    engine, _ = _engine_and_ws()
    with engine.begin() as conn:
        _write_settings(
            conn,
            settings=settings_payload,
            touches=touches_payload,
        )
        data = _read_settings(conn)
    return CadenceSettingsView(**{
        k: v for k, v in data.items() if not k.startswith("_")
    })


@router.post(
    "/settings/cadence/preset",
    response_model=CadenceSettingsView,
    summary="Replace cadence with one of the named presets",
)
def apply_preset(
    body: PresetBody,
    _auth: None = Depends(require_auth),
) -> CadenceSettingsView:
    preset_name = (body.preset or "").strip().lower()
    if preset_name not in _VALID_PRESETS:
        raise HTTPException(
            422,
            f"preset must be one of {sorted(_VALID_PRESETS)}; "
            f"got {body.preset!r}",
        )
    preset = _PRESETS[preset_name]
    engine, _ = _engine_and_ws()
    with engine.begin() as conn:
        # Preserve existing booleans (enabled / paused / auto_*)
        # so flipping presets doesn't silently reset the
        # operator's pause + auto-stop preferences.
        existing = _read_settings(conn)
        _write_settings(
            conn,
            settings={
                "enabled": existing["enabled"],
                "paused": existing["paused"],
                "max_touches": preset["max_touches"],
                "daily_mix_new_pct": preset["daily_mix_new_pct"],
                "auto_stop_on_reply": existing["auto_stop_on_reply"],
                "auto_stop_on_pipeline_advance": existing[
                    "auto_stop_on_pipeline_advance"
                ],
                "auto_stop_on_manual_pass": existing["auto_stop_on_manual_pass"],
                "auto_stop_on_fund_news": existing["auto_stop_on_fund_news"],
            },
            touches=preset["touches"],
        )
        data = _read_settings(conn)
    return CadenceSettingsView(**{
        k: v for k, v in data.items() if not k.startswith("_")
    })


@router.post(
    "/settings/cadence/pause",
    response_model=CadenceSettingsView,
    summary=(
        "Flip the global cadence pause flag (preserves touches "
        "+ everything else)"
    ),
)
def pause_cadence(
    body: PauseBody,
    _auth: None = Depends(require_auth),
) -> CadenceSettingsView:
    engine, _ = _engine_and_ws()
    with engine.begin() as conn:
        existing = _read_settings(conn)
        existing["paused"] = bool(body.paused)
        _write_settings(
            conn,
            settings={
                k: v for k, v in existing.items()
                if k not in ("touches", "updated_at", "_seeded")
            },
            touches=existing["touches"],
        )
        data = _read_settings(conn)
    return CadenceSettingsView(**{
        k: v for k, v in data.items() if not k.startswith("_")
    })
