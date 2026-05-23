from __future__ import annotations

import importlib.util
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_stage6():
    spec = importlib.util.spec_from_file_location(
        "stage6_score_candidates",
        REPO_ROOT / "scripts" / "06_score_candidates.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_unknown_cold_reachability_is_not_recommended():
    stage6 = _load_stage6()

    send_now = stage6.compute_send_now_priority(
        round_fit_score=8.0,
        lead_likelihood_score=8.0,
        composite_fit_score=8.0,
        cold_reachability_score=None,
        spiky_belief_score=0.0,
        recency_bonus=0.0,
        major_kill=False,
    )
    assert send_now == 36.0

    recommended, reason = stage6.evaluate_recommended(
        composite=8.0,
        round_fit_score=8.0,
        disqualifier_present=False,
        lead_likelihood_score=8.0,
        distinct_source_types=2,
        q2_plus_signal_count=2,
        deal_attribution_count=0,
        most_recent_signal_date=date(2026, 5, 1),
        employment_status="verified_current",
        major_kill=False,
        cold_reachability_score=None,
        warm_path_available=False,
        today=date(2026, 5, 23),
    )
    assert recommended is False
    assert "cold_reachability_score is unknown" in reason
