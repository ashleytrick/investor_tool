"""Stage 4 partner signal mining split into discrete steps (Slice 18c /
REFACTOR_PLAN item 6).

Splits the fetch + extract concerns out of
`scripts/04_mine_partner_signals.py` so each step is independently
testable:

  - core.stage4.fetch    : upsert_snapshot, upsert_snapshot_failure,
                            _fetch_live_partner_content
  - core.stage4.extract  : render_prompt + LLM extraction helpers

Apply + reconcile (signal upsert, reachability persistence, stale-
content reconciliation) already live in core.partner_evidence
(Refactor item 12). The Stage 4 script is now a thin orchestrator.
"""
