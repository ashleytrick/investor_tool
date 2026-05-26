"""Stage 2 fund enrichment split into discrete steps (Slice 18c /
REFACTOR_PLAN item 6).

The script `scripts/02_enrich_funds.py` used to bundle fetch + extract
+ apply + reconcile in one ~420-line file. This package splits the
fetch/extract concerns into testable modules:

  - core.stage2.fetch    : gather_fixture_pages, gather_live_pages,
                            store_snapshots
  - core.stage2.extract  : enrich (LLM), deterministic_enrichment
                            (fixture stub)

The apply + reconcile pieces already live in
`core.fund_enrichment` (Refactor item 11). The script is now a thin
orchestrator over these modules.
"""
