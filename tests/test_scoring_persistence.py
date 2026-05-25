"""Unit tests for core/scoring/persistence.py (Refactor item 7 / 13)."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from tests.conftest import REPO_ROOT  # noqa: F401 - sys.path side-effect

import pytest
from sqlalchemy import select

from core.db import (
    force_refresh_log,
    funds,
    get_engine,
    partner_score_summaries,
    partners,
    scores as scores_table,
)
from core.scoring.persistence import (
    log_force_refresh_diff, persist_partner_score,
)


@pytest.fixture
def engine(tmp_path: Path):
    """Spin up an isolated SQLite via the project's get_engine (which
    handles the column-sync metadata) so we can write into the real
    tables without touching the workspace fixture.

    Also seeds the (fund -> partner) parent rows that
    partner_score_summaries' FK requires.
    """
    db_path = tmp_path / "test.db"
    eng = get_engine(f"sqlite:///{db_path}")
    with eng.begin() as conn:
        conn.execute(funds.insert().values(
            fund_id="f1", name="Test Fund", domain="test.example",
            is_active=True,
        ))
        conn.execute(partners.insert().values(
            partner_id="p1", fund_id="f1", name="Test Partner",
        ))
    return eng


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _values(**over) -> dict:
    """Minimum kwargs for partner_score_summaries upsert."""
    base = dict(
        partner_id="p1",
        composite_fit_score=7.5,
        round_fit_score=8.0,
        lead_likelihood_score=6.0,
        cold_reachability_score=7.0,
        send_now_priority=28.0,
        recommended_to_send=True,
        recommendation_reasoning="ok",
        scored_at=_now(),
    )
    base.update(over)
    return base


def _axis(score, confidence="medium", supporting=()):
    return SimpleNamespace(
        score=score,
        confidence=confidence,
        supporting_signal_ids=list(supporting),
    )


# ----- persist_partner_score -----


def test_upsert_creates_row_when_partner_new(engine) -> None:
    persist_partner_score(
        engine,
        partner_id="p1",
        summary_values=_values(),
        axis_scores={
            "axis_a": _axis(8.0, supporting=[1, 2]),
            "axis_b": _axis(None),  # None must be skipped
        },
    )
    with engine.begin() as conn:
        rows = list(conn.execute(
            select(partner_score_summaries.c.partner_id,
                   partner_score_summaries.c.send_now_priority)
        ))
        score_rows = list(conn.execute(
            select(scores_table.c.axis_id, scores_table.c.score)
        ))
    assert rows == [("p1", 28.0)]
    # Only axis_a should land; axis_b was skipped because score=None.
    assert score_rows == [("axis_a", 8.0)]


def test_upsert_updates_existing_row(engine) -> None:
    persist_partner_score(
        engine, partner_id="p1",
        summary_values=_values(send_now_priority=10.0),
        axis_scores={"axis_a": _axis(5.0)},
    )
    persist_partner_score(
        engine, partner_id="p1",
        summary_values=_values(send_now_priority=42.0),
        axis_scores={"axis_a": _axis(9.0)},
    )
    with engine.begin() as conn:
        rows = list(conn.execute(
            select(partner_score_summaries.c.send_now_priority)
        ))
        score_rows = list(conn.execute(
            select(scores_table.c.score)
        ))
    assert rows == [(42.0,)]
    # Old axis row replaced (delete + insert in same transaction).
    assert score_rows == [(9.0,)]


def test_re_persist_replaces_per_axis_rows(engine) -> None:
    """Bug guard: if we wrote (axis_a, axis_b) then re-ran with just
    axis_a, axis_b must NOT persist as a stale row."""
    persist_partner_score(
        engine, partner_id="p1", summary_values=_values(),
        axis_scores={
            "axis_a": _axis(5.0),
            "axis_b": _axis(6.0),
        },
    )
    persist_partner_score(
        engine, partner_id="p1", summary_values=_values(),
        axis_scores={"axis_a": _axis(5.0)},
    )
    with engine.begin() as conn:
        score_rows = list(conn.execute(
            select(scores_table.c.axis_id)
        ))
    assert score_rows == [("axis_a",)]


def test_skipped_axis_score_does_not_land(engine) -> None:
    persist_partner_score(
        engine, partner_id="p1", summary_values=_values(),
        axis_scores={
            "axis_a": _axis(None),
            "axis_b": _axis(None),
        },
    )
    with engine.begin() as conn:
        score_rows = list(conn.execute(select(scores_table.c.axis_id)))
    assert score_rows == []


# ----- log_force_refresh_diff -----


def test_force_refresh_logs_each_changed_field(engine) -> None:
    persist_partner_score(
        engine, partner_id="p1",
        summary_values=_values(send_now_priority=10.0,
                                recommended_to_send=False,
                                recommendation_reasoning="old"),
        axis_scores={},
    )
    with engine.begin() as conn:
        existing = conn.execute(
            select(partner_score_summaries).where(
                partner_score_summaries.c.partner_id == "p1"
            )
        ).first()
    new_vals = _values(send_now_priority=42.0,
                       recommended_to_send=True,
                       recommendation_reasoning="new",
                       scored_at=_now())
    written = log_force_refresh_diff(
        engine, partner_id="p1", existing=existing,
        new_values=new_vals, reason="operator override",
    )
    assert written == 3  # 3 changed fields, scored_at excluded
    with engine.begin() as conn:
        log_rows = list(conn.execute(
            select(force_refresh_log.c.field_name, force_refresh_log.c.reason)
            .where(force_refresh_log.c.partner_id == "p1")
        ))
    fields = sorted(r[0] for r in log_rows)
    assert fields == sorted([
        "send_now_priority",
        "recommended_to_send",
        "recommendation_reasoning",
    ])
    assert all(r[1] == "operator override" for r in log_rows)


def test_force_refresh_excludes_scored_at(engine) -> None:
    """scored_at always changes on every re-run; logging it would
    drown the audit in noise. The skip_fields default excludes it."""
    persist_partner_score(
        engine, partner_id="p1", summary_values=_values(), axis_scores={},
    )
    with engine.begin() as conn:
        existing = conn.execute(
            select(partner_score_summaries).where(
                partner_score_summaries.c.partner_id == "p1"
            )
        ).first()
    # Same values except scored_at -> no log rows.
    new_vals = _values(scored_at=_now())
    written = log_force_refresh_diff(
        engine, partner_id="p1", existing=existing,
        new_values=new_vals, reason="recheck",
    )
    assert written == 0


def test_force_refresh_no_diffs_no_logs(engine) -> None:
    persist_partner_score(
        engine, partner_id="p1", summary_values=_values(), axis_scores={},
    )
    with engine.begin() as conn:
        existing = conn.execute(
            select(partner_score_summaries).where(
                partner_score_summaries.c.partner_id == "p1"
            )
        ).first()
    written = log_force_refresh_diff(
        engine, partner_id="p1", existing=existing,
        new_values=_values(scored_at=existing.scored_at),
        reason="recheck",
    )
    assert written == 0


def test_atomic_audit_writes_after_persist_succeeds(engine) -> None:
    """Launch-blocker fix: passing force_refresh_audit to
    persist_partner_score writes the audit rows in the SAME
    transaction as the persistence, so a persist failure rolls back
    the audit."""
    persist_partner_score(
        engine, partner_id="p1",
        summary_values=_values(send_now_priority=10.0),
        axis_scores={},
    )
    with engine.begin() as conn:
        existing = conn.execute(
            select(partner_score_summaries).where(
                partner_score_summaries.c.partner_id == "p1"
            )
        ).first()
    new_vals = _values(send_now_priority=42.0, scored_at=_now())
    written = persist_partner_score(
        engine, partner_id="p1", summary_values=new_vals,
        axis_scores={},
        force_refresh_audit={"existing": existing, "reason": "override"},
    )
    assert written == 1
    with engine.begin() as conn:
        score_now = conn.execute(
            select(partner_score_summaries.c.send_now_priority)
        ).scalar()
        audit_rows = list(conn.execute(
            select(force_refresh_log.c.field_name)
        ))
    assert score_now == 42.0
    assert audit_rows == [("send_now_priority",)]


def test_atomic_audit_rolls_back_when_persist_fails(engine) -> None:
    """If the per-axis insert raises mid-transaction, NO audit row
    should land -- the previous separate-transaction shape would have
    written the audit BEFORE the persist crashed, claiming an
    override was broken even though no new score landed."""
    persist_partner_score(
        engine, partner_id="p1", summary_values=_values(), axis_scores={},
    )
    with engine.begin() as conn:
        existing = conn.execute(
            select(partner_score_summaries).where(
                partner_score_summaries.c.partner_id == "p1"
            )
        ).first()

    class _Unserializable:
        pass

    class _BadAxis:
        score = 5.0
        confidence = "medium"
        # json.dumps will raise on a class instance with no method.
        supporting_signal_ids = _Unserializable()

    try:
        persist_partner_score(
            engine, partner_id="p1",
            summary_values=_values(send_now_priority=99.0),
            axis_scores={"axis_a": _BadAxis()},
            force_refresh_audit={
                "existing": existing, "reason": "should rollback",
            },
        )
    except Exception:
        pass  # expected
    with engine.begin() as conn:
        score_now = conn.execute(
            select(partner_score_summaries.c.send_now_priority)
        ).scalar()
        audit_rows = list(conn.execute(
            select(force_refresh_log.c.field_name)
        ))
    # Score still at the prior value; no audit row landed despite
    # send_now_priority "changing" in new_values.
    assert score_now != 99.0
    assert audit_rows == []
