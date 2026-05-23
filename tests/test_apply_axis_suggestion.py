from __future__ import annotations

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_apply_job():
    spec = importlib.util.spec_from_file_location(
        "apply_axis_suggestion",
        REPO_ROOT / "jobs" / "apply_axis_suggestion.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_low_confidence_refusal_message_for_bulk_apply():
    job = _load_apply_job()

    message = job.low_confidence_refusal_message(
        mode="--all-above low",
        count=2,
    )

    assert message == (
        "REFUSED: --all-above low would apply 2 confidence='low' suggestions. "
        "Re-run with --accept-low-confidence to override."
    )


def test_low_confidence_refusal_message_for_single_apply():
    job = _load_apply_job()

    message = job.low_confidence_refusal_message(
        mode="single",
        suggestion_id=17,
        sample_size=3,
    )

    assert message == (
        "REFUSED: suggestion_id=17 has confidence='low' (sample_size=3). "
        "Re-run with --accept-low-confidence to override."
    )
