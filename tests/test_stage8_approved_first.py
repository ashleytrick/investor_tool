"""Regression test for the Stage 8 partner-selection finding.

Under --require-ready-to-send, the partner loop must select the top N
APPROVED live drafts by score, not the top N partners by score then
filter out the unapproved ones. The earlier behavior could sync 0
partners even when the workspace had approvals -- the approved partner
might rank #26 by send_now_priority and the --top 25 cap silently
dropped them.

Setup: approve exactly ONE recommended draft for the partner with the
LOWEST send_now_priority among the recommended set. Then run Stage 8
with --require-ready-to-send --top 1. The approved partner must be
the one that gets synced, despite being below the top-N score window
under the old logic.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

from tests.conftest import REPO_ROOT, _run, _run_pipeline_through_stage_6


def test_stage8_require_ready_picks_approved_below_top_n():
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_dst = Path(tmpdir) / "test_workspace"
        shutil.copytree(ws_src, ws_dst)
        db = ws_dst / "data" / "pipeline.db"
        if db.exists():
            db.unlink()
        _run_pipeline_through_stage_6(ws_dst)
        ws = str(ws_dst)
        _run(
            "07_generate_emails.py", "--workspace", ws,
            "--top", "5", "--allow-example-domains", cwd=REPO_ROOT,
        )

        # Pick the recommended draft for the LOWEST-priority partner
        # (so the old logic with --top 1 would never even consider
        # this partner). Approve it.
        c = sqlite3.connect(db)
        rows = c.execute(
            "select d.draft_id, d.partner_id, s.send_now_priority "
            "from email_drafts d "
            "join partner_score_summaries s on s.partner_id=d.partner_id "
            "where d.is_recommended=1 and d.superseded_at is null "
            "order by s.send_now_priority asc"
        ).fetchall()
        assert len(rows) >= 2, (
            "fixture should produce >=2 recommended drafts so this "
            "test can express 'lowest score'"
        )
        lowest_draft_id, lowest_pid, lowest_score = rows[0]
        top_pid = rows[-1][1]
        # Set partner email + valid verification so the approval
        # passes the gate trivially.
        for (_, pid, _) in rows:
            c.execute(
                "update partners set email=? || '@op.com', "
                "email_verification_status='valid' where partner_id=?",
                (pid.replace(".", "_").replace(":", "_"), pid),
            )
        c.execute(
            "update email_drafts set approval_status='approved_to_send' "
            "where draft_id=?", (lowest_draft_id,),
        )
        c.execute(
            "insert into draft_approvals(draft_id, partner_id, "
            "event_type, actor, at, draft_hash) values (?, ?, "
            "'approved_to_send', 'tester', datetime('now'), 'h')",
            (lowest_draft_id, lowest_pid),
        )
        c.commit()
        c.close()

        # Minimal attio.yaml.
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

        # Drive Stage 8 with a stubbed Attio client that records each
        # create/update call so the test can assert WHO got synced.
        driver = ws_dst / "_drive_stage8_approved_first.py"
        driver.write_text(
            "import sys, importlib.util, itertools, json\n"
            f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
            "import core.attio_client as ac\n"
            "from core.attio_client import AttioClient\n"
            "_co = itertools.count(); _per = itertools.count()\n"
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
            "        with open('/tmp/stage8_approved_first_calls.json', 'w') as f:\n"
            "            json.dump(calls, f, default=str)\n"
            "ac.AttioClient.from_workspace = classmethod(lambda cls, ws: FakeClient())\n"
            "spec = importlib.util.spec_from_file_location("
            f"'s8', {str(REPO_ROOT / 'scripts' / '08_sync_to_attio.py')!r})\n"
            "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
            "m.find_partner_record = lambda *a, **kw: None\n"
            "m.AttioClient.from_workspace = classmethod(lambda cls, ws: FakeClient())\n"
            f"sys.argv = ['s8', '--workspace', {ws!r}, '--top', '1', "
            f"'--require-ready-to-send', '--allow-example-domains', "
            f"'--allow-fixture-mode']\n"
            "raise SystemExit(m.main())\n"
        )
        env = {
            **os.environ, "ANTHROPIC_API_KEY": "", "ATTIO_API_KEY": "fake-key",
        }
        res = subprocess.run(
            [sys.executable, str(driver)],
            capture_output=True, text=True, env=env, timeout=120,
        )
        assert res.returncode == 0, res.stdout + res.stderr

        # Exactly ONE partner should have been synced (--top 1) and
        # it must be the approved one (lowest score), not the top
        # partner the old logic would have picked then skipped.
        c = sqlite3.connect(db)
        pushed = c.execute(
            "select partner_id from email_drafts "
            "where pushed_to_attio_at is not null"
        ).fetchall()
        c.close()
        assert pushed, f"expected the approved draft to be pushed; got {pushed}"
        pushed_pids = {r[0] for r in pushed}
        assert lowest_pid in pushed_pids, (
            f"approved low-priority partner {lowest_pid!r} should "
            f"have been synced under --require-ready-to-send; "
            f"pushed={pushed_pids}"
        )
        assert top_pid not in pushed_pids, (
            f"top-priority unapproved partner {top_pid!r} should NOT "
            f"have been synced; pushed={pushed_pids}"
        )
