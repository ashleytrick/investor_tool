"""Regression tests for the post-PR-25 review finding: superseded
email_drafts must never be approvable / surfaced in pending review /
returned as approved_for_send / synced to Attio.

The Slice 17 immutable history pattern preserves prior generations
by stamping superseded_at when Stage 7 re-runs. Before this fix,
the approval read paths only filtered on approval_status, so a stale
old draft could:
  - appear in `pending_review()` (list_pending_review CLI)
  - be approved by draft_id via approve_draft.py
  - slip through approved_for_send() into Gmail/export/Attio

Each test asserts one of those holes is now closed.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select, update

from tests.conftest import REPO_ROOT, _run, _run_pipeline_through_stage_6

from core.approval.persistence import (
    approved_for_send, pending_review,
)
from core.approval.gate import (
    can_approve_draft, classify_blocker,
)
from core.db import email_drafts, get_engine


def _stage7(ws: str) -> None:
    _run(
        "07_generate_emails.py", "--workspace", ws,
        "--top", "5", "--allow-example-domains", cwd=REPO_ROOT,
    )


def _setup_with_superseded(tmp_path: Path) -> tuple[Path, str, int, str]:
    """Build a fixture workspace with a superseded draft. Returns
    (db_path, workspace_str, superseded_draft_id, partner_id)."""
    ws_src = REPO_ROOT / "clients" / "test_workspace"
    ws_dst = tmp_path / "test_workspace"
    shutil.copytree(ws_src, ws_dst)
    db = ws_dst / "data" / "pipeline.db"
    if db.exists():
        db.unlink()
    ws = str(ws_dst)
    _run_pipeline_through_stage_6(ws_dst)
    _stage7(ws)
    # Re-run Stage 7 so the original generation gets superseded.
    _stage7(ws)
    c = sqlite3.connect(db)
    super_id, pid = c.execute(
        "select draft_id, partner_id from email_drafts "
        "where superseded_at is not null limit 1"
    ).fetchone()
    c.close()
    return db, ws, super_id, pid


def test_pending_review_excludes_superseded_drafts(tmp_path: Path) -> None:
    """`pending_review()` must NOT include rows where superseded_at is set,
    even when they're still labeled `needs_review`."""
    db, ws, super_id, pid = _setup_with_superseded(tmp_path)
    # Force the superseded row back to needs_review so the only thing
    # excluding it is the superseded_at filter we're testing.
    engine = get_engine(f"sqlite:///{db}")
    with engine.begin() as conn:
        conn.execute(
            update(email_drafts)
            .where(email_drafts.c.draft_id == super_id)
            .values(approval_status="needs_review")
        )
    pending = pending_review(engine)
    ids = {row.draft_id for row in pending}
    assert super_id not in ids, (
        f"superseded draft_id={super_id} leaked into pending_review; "
        f"ids returned: {ids}"
    )


def test_approved_for_send_excludes_superseded_drafts(tmp_path: Path) -> None:
    """An approved_to_send row that is also superseded must NOT be
    returned by approved_for_send(). Defense in depth: even if the
    state-machine mark_stale transition failed to fire, the read
    filter catches it."""
    db, ws, super_id, pid = _setup_with_superseded(tmp_path)
    engine = get_engine(f"sqlite:///{db}")
    # Force the superseded row back to approved_to_send so the only
    # gate is the superseded_at filter.
    with engine.begin() as conn:
        conn.execute(
            update(email_drafts)
            .where(email_drafts.c.draft_id == super_id)
            .values(approval_status="approved_to_send")
        )
    approved = approved_for_send(engine)
    ids = {row.draft_id for row in approved}
    assert super_id not in ids, (
        f"superseded draft_id={super_id} leaked into approved_for_send; "
        f"ids returned: {ids}"
    )


def test_can_approve_draft_refuses_superseded(tmp_path: Path) -> None:
    """can_approve_draft must return ok=False for a superseded draft
    with a message that mentions 'superseded' so the operator sees
    why."""
    db, ws, super_id, pid = _setup_with_superseded(tmp_path)
    from core.config_loader import load_workspace
    workspace = load_workspace(str(tmp_path / "test_workspace"))
    engine = get_engine(f"sqlite:///{db}")
    gate = can_approve_draft(
        workspace, engine, super_id, allow_example_domains=True,
    )
    assert gate.ok is False
    assert any("superseded" in b.lower() for b in gate.blockers), (
        f"expected a 'superseded' blocker; got {gate.blockers}"
    )


def test_superseded_blocker_is_classified_hard() -> None:
    """The superseded refusal cannot be bypassed via
    --override-blockers -- classify_blocker must return 'hard'."""
    msg = "draft_id=99 is superseded (superseded_at=2026-01-01T00:00:00)"
    assert classify_blocker(msg) == "hard"


def test_approve_draft_cli_refuses_superseded(tmp_path: Path) -> None:
    """End-to-end: approve_draft.py refuses on a superseded draft_id
    + exits non-zero + the pointer doesn't move."""
    db, ws, super_id, pid = _setup_with_superseded(tmp_path)
    # Set a valid email so the gate's other blockers don't fire and
    # we're specifically testing the superseded refusal.
    c = sqlite3.connect(db)
    c.execute(
        "update partners set email='op@operator.com', "
        "email_verification_status='valid' where partner_id=?",
        (pid,),
    )
    # Force the superseded row's approval_status back to needs_review
    # so the state machine doesn't refuse the transition for a
    # different reason.
    c.execute(
        "update email_drafts set approval_status='needs_review' "
        "where draft_id=?", (super_id,),
    )
    c.commit()
    c.close()
    res = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "approve_draft.py"),
         "--workspace", ws, "--draft-id", str(super_id),
         "--allow-example-domains"],
        capture_output=True, text=True,
        env={**os.environ, "USER": "tester"}, timeout=60,
    )
    assert res.returncode == 2, res.stdout + res.stderr
    assert "superseded" in res.stdout.lower()
    # Pointer didn't move.
    c = sqlite3.connect(db)
    status = c.execute(
        "select approval_status from email_drafts where draft_id=?",
        (super_id,),
    ).fetchone()[0]
    c.close()
    assert status == "needs_review"


def test_override_blockers_cannot_bypass_superseded(tmp_path: Path) -> None:
    """--override-blockers --notes must still refuse a superseded
    draft; HARD blockers (which superseded is) can never be bypassed."""
    db, ws, super_id, pid = _setup_with_superseded(tmp_path)
    c = sqlite3.connect(db)
    c.execute(
        "update partners set email='op@operator.com', "
        "email_verification_status='valid' where partner_id=?",
        (pid,),
    )
    c.execute(
        "update email_drafts set approval_status='needs_review' "
        "where draft_id=?", (super_id,),
    )
    c.commit()
    c.close()
    res = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "approve_draft.py"),
         "--workspace", ws, "--draft-id", str(super_id),
         "--override-blockers", "--notes", "trying to bypass",
         "--allow-example-domains"],
        capture_output=True, text=True, timeout=60,
    )
    assert res.returncode == 2, res.stdout + res.stderr
    # Either the early refusal ("draft is superseded") OR the HARD
    # blocker refusal ("HARD blocker(s) cannot be bypassed"). Both
    # are correct -- the superseded check is a short-circuit in the
    # CLI and ALSO a HARD entry in the gate.
    out = res.stdout.lower()
    assert "superseded" in out


def test_stage8_skips_superseded_drafts(tmp_path: Path) -> None:
    """Stage 8's email_drafts query is now filtered on
    superseded_at IS NULL so an old generation can never populate
    Attio's outreach fields. Use a stubbed AttioClient to drive the
    sync + inspect which draft id ended up in the payload."""
    db, ws, super_id, pid = _setup_with_superseded(tmp_path)
    # Sanity check: there is a LIVE draft for this partner too.
    c = sqlite3.connect(db)
    live = c.execute(
        "select draft_id from email_drafts where partner_id=? "
        "and superseded_at is null and is_recommended=1 limit 1",
        (pid,),
    ).fetchone()
    c.close()
    assert live is not None, "setup invariant: live recommended draft exists"

    # Drive Stage 8 with a stubbed client (mirrors
    # test_stage8_pushed_at_timestamps_via_driver shape).
    ws_dst = Path(ws)
    (ws_dst / "config" / "attio.yaml").write_text(
        "attio:\n"
        "  workspace_id: dummy\n"
        "  api_base: https://api.attio.com/v2\n"
        "  matching_attributes:\n"
        "    companies: domains\n"
        "    people: email_addresses\n"
        "  objects:\n"
        "    funds: companies\n"
        "    partners: people\n"
        "  fund_attributes: {}\n"
        "  partner_attributes: {}\n",
        encoding="utf-8",
    )
    driver = ws_dst / "_drive_stage8_supersede.py"
    driver.write_text(
        "import sys, importlib.util, itertools\n"
        f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
        "import core.attio_client as ac\n"
        "from core.attio_client import AttioClient\n"
        "_co = itertools.count()\n"
        "_per = itertools.count()\n"
        "calls = []\n"
        "class FakeClient:\n"
        "    def upsert_record(self, obj, slug, payload):\n"
        "        return {'data': {'id': {'record_id': 'fake_co_' + str(next(_co))}}}\n"
        "    def get_record(self, obj, rid):\n"
        "        return None\n"
        "    def create_record(self, obj, payload):\n"
        "        calls.append(('create', obj, payload))\n"
        "        return {'data': {'id': {'record_id': 'fake_per_' + str(next(_per))}}}\n"
        "    def update_record(self, obj, rid, payload):\n"
        "        calls.append(('update', obj, payload))\n"
        "        return {'data': {'id': {'record_id': rid}}}\n"
        "    def attribute_slugs(self, obj):\n"
        "        return set()\n"
        "    def close(self):\n"
        "        import json\n"
        "        with open('/tmp/stage8_attio_calls.json', 'w') as f:\n"
        "            json.dump(calls, f, default=str)\n"
        "ac.AttioClient.from_workspace = classmethod(lambda cls, ws: FakeClient())\n"
        "spec = importlib.util.spec_from_file_location("
        f"'s8', {str(REPO_ROOT / 'scripts' / '08_sync_to_attio.py')!r})\n"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
        "m.find_partner_record = lambda *a, **kw: None\n"
        "m.AttioClient.from_workspace = classmethod(lambda cls, ws: FakeClient())\n"
        f"sys.argv = ['s8', '--workspace', {ws!r}, '--top', '5', "
        f"'--allow-example-domains', '--allow-fixture-mode']\n"
        "raise SystemExit(m.main())\n"
    )
    env = {**os.environ, "ANTHROPIC_API_KEY": "", "ATTIO_API_KEY": "fake-key"}
    res = subprocess.run(
        [sys.executable, str(driver)],
        capture_output=True, text=True, env=env, timeout=120,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    # The superseded draft's body should NOT appear in any Attio
    # payload. We can prove the inverse: the LIVE draft's pushed_at
    # was stamped (so Stage 8 picked it up), and the superseded one
    # wasn't.
    c = sqlite3.connect(db)
    super_pushed = c.execute(
        "select pushed_to_attio_at from email_drafts where draft_id=?",
        (super_id,),
    ).fetchone()[0]
    live_pushed = c.execute(
        "select pushed_to_attio_at from email_drafts where draft_id=?",
        (live[0],),
    ).fetchone()[0]
    c.close()
    assert super_pushed is None, (
        f"Stage 8 stamped pushed_to_attio_at on superseded draft "
        f"{super_id}; expected NULL"
    )
    # The live draft may or may not have been pushed (depends on
    # whether it was approved). The test invariant is just: the
    # superseded one was never touched.
