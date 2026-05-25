"""End-to-end + per-stage tests have been split out (Refactor item 23).

Use the per-stage files instead:
  - tests/conftest.py                shared helpers + workspace fixtures
  - tests/test_pipeline_e2e.py       full-pipeline + cross-stage invariants
  - tests/test_stage1_sources.py     Stage 1 source aggregation
  - tests/test_stage2_enrichment.py  Stage 2 fund enrichment
  - tests/test_stage3_attribution.py Stage 3 deal attribution
  - tests/test_stage4_signals.py     Stage 4 partner signals
  - tests/test_stage5_verification.py Stage 5 verification + quality
  - tests/test_stage6_scoring.py     Stage 6 scoring + recommendation
  - tests/test_stage7_email_qa.py    Stage 7 email generation + QA
  - tests/test_stage8_attio.py       Stage 8 Attio sync
  - tests/test_jobs.py               jobs/ (learning report, axis apply)
  - tests/test_operator_clis.py      scripts/ CLI tools
  - tests/test_config_and_validators.py config validation + utility tests
  - tests/test_refactor_helpers.py   stage_run + RunLogger.attempt() tests
"""
