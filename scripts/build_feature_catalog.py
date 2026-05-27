"""Generate docs/FEATURE_CATALOG.md from the live FastAPI app.

For every HTTP route registered on the app, looks up:
  - HTTP method + path
  - The handler function and its module
  - Whether any test file mentions the path (rough proxy for coverage)
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# Avoid blocking imports.
os.environ.setdefault("API_KEY", "catalog")
os.environ.setdefault("CORS_ORIGINS", "https://example.com")
os.environ.setdefault("INVESTOR_WORKSPACE", str(REPO / "clients" / "test_workspace"))

from web.api import app  # noqa: E402


def grep_tests_for_path(
    tests_dir: Path, path: str, handler_name: str,
) -> list[str]:
    """Return test files that plausibly cover this endpoint.

    Three signals (any one counts as a hit):
      1. The path with `{param}` placeholders replaced by `.+`
         appears in the test (regex match) — catches f-string call
         sites like `f"/drafts/{draft_id}/mark-sent"`.
      2. The path prefix (everything up to the first `{`) appears
         literally — catches partial-path mentions.
      3. The handler function name appears in the test — catches
         tests that import + call the handler directly.
    """
    # 1. Regex on the parametrized path.
    regex = re.compile(re.sub(r"\{[^}]+\}", r"[^\"/' ]+", path))
    # 2. Path prefix (before any `{`).
    prefix_split = path.split("{", 1)[0].rstrip("/")
    prefix_needle = (
        prefix_split if len(prefix_split) >= 5 else None
    )
    matches: list[str] = []
    for test in sorted(tests_dir.glob("test_*.py")):
        try:
            text = test.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        hit = (
            regex.search(text) is not None
            or (prefix_needle and prefix_needle in text)
            or (handler_name and handler_name in text)
        )
        if hit:
            matches.append(test.name)
    return matches


def grep_tests_for_symbol(tests_dir: Path, symbol: str) -> list[str]:
    """Return test files mentioning the symbol (function or
    module name). Used for non-HTTP surfaces.

    Note: tests that exercise a handler via the HTTP surface
    (`client.post(...)`) won't import the handler function by
    name, so this misses real coverage in those cases. Use the
    HTTP catalog above for endpoint-level coverage and treat this
    section as 'is there a direct unit test for this internal?'.
    """
    if not symbol:
        return []
    matches: list[str] = []
    for test in sorted(tests_dir.glob("test_*.py")):
        try:
            text = test.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if symbol in text:
            matches.append(test.name)
    return matches


# Non-HTTP surfaces grouped by area. Each entry =
# (subarea, name, symbol_to_grep, purpose).
_NON_HTTP_SURFACES: list[tuple[str, str, str, str]] = [
    # ---- Pipeline stages ----
    (
        "Pipeline stages",
        "Stage 1 — aggregate_sources",
        "01_aggregate_sources",
        "Load RSS / funding-announcement feeds into source_snapshots",
    ),
    (
        "Pipeline stages",
        "Stage 2 — enrich_funds",
        "02_enrich_funds",
        "Scrape fund homepage / portfolio / team; LLM-extract thesis",
    ),
    (
        "Pipeline stages",
        "Stage 3 — mine_activity",
        "03_mine_activity",
        "LLM-attribute funding announcements to funds + partners",
    ),
    (
        "Pipeline stages",
        "Stage 4 — mine_partner_signals",
        "04_mine_partner_signals",
        "Scrape LinkedIn/podcasts/blogs; LLM extract signals",
    ),
    (
        "Pipeline stages",
        "Stage 5 — verify_and_quality",
        "05_verify_and_quality",
        "Re-fetch signal URLs + deterministic 0-3 quality scoring",
    ),
    (
        "Pipeline stages",
        "Stage 6 — score_candidates",
        "06_score_candidates",
        "Composite fit + round fit + lead likelihood + send-now priority",
    ),
    (
        "Pipeline stages",
        "Stage 7 — generate_emails",
        "07_generate_emails",
        "LLM email drafts (6 strategies × 2 variants) + batch QA",
    ),
    (
        "Pipeline stages",
        "Stage 8 — sync_to_attio",
        "08_sync_to_attio",
        "Push partners + scores + outreach status to Attio CRM",
    ),
    # ---- Email drafting subsystems ----
    (
        "Drafting",
        "Strategy eligibility (6 strategies)",
        "compute_eligibility",
        "Score each of the 6 strategies 0-3 for a partner",
    ),
    (
        "Drafting",
        "Batch QA — similarity",
        "ratio_similarity",
        "Pairwise body/subject/first-sentence similarity hard gates",
    ),
    (
        "Drafting",
        "Batch QA — template smell",
        "template_smell_judge",
        "LLM (or heuristic in stub) judges template-smell vs neighbors",
    ),
    (
        "Drafting",
        "Banned-phrase + hard-gate enforcement",
        "check_hard_gates",
        "Forbidden phrases, raise reference, soft CTA, etc.",
    ),
    (
        "Drafting",
        "Operator voice samples (Stage 7 prompt injection)",
        "load_voice_samples_for_prompt",
        "Up to 3 most-recent samples injected as OPERATOR_VOICE_SAMPLES",
    ),
    # ---- Approval state machine ----
    (
        "Approval",
        "State machine — transitions table",
        "_TRANSITIONS",
        "11 declared edges; STATE_SENT is terminal",
    ),
    (
        "Approval",
        "Approval gate — can_approve_draft",
        "can_approve_draft",
        "Hard (DNC, bad email, smell) + soft blockers; override flag",
    ),
    (
        "Approval",
        "Stale-after-approval invalidation",
        "stale_live_approvals_for_partner",
        "Email change / DNC / relationship / score change triggers",
    ),
    # ---- Sequences / cadence / follow-ups ----
    (
        "Sequences",
        "auto_stop_sequence_if_active",
        "auto_stop_sequence_if_active",
        "Idempotent cadence-gated sequence stop",
    ),
    (
        "Sequences",
        "Reply auto-stop wiring (B3 reconcile)",
        "reconcile_drafts_for_workspace",
        "Joins outreach_events to sequences; only post-create replies",
    ),
    (
        "Sequences",
        "CRM-pipeline auto-stop wiring (B6 poll)",
        "poll_crm_pipeline_for_workspace",
        "Compares prior vs new stage; fires only on change",
    ),
    (
        "Sequences",
        "Cadence presets (standard / patient / aggressive)",
        "_PRESETS",
        "Three preset shapes accessible via /settings/cadence/preset",
    ),
    (
        "Sequences",
        "Follow-up draft builder (FR-5)",
        "build_follow_ups_for_workspace",
        "Daily build; prior-send gate; implied due-at; per-angle prompts",
    ),
    # ---- Meeting prep / dossier ----
    (
        "Meeting prep",
        "Dossier builder",
        "core.meeting_prep.dossier",
        "LLM-driven dossier; eligibility-gated; cache by signal-set hash",
    ),
    (
        "Meeting prep",
        "Drive push (idempotent)",
        "drive_sync",
        "Upload to operator's Drive; skip if already pushed for the hash",
    ),
    # ---- Deck extraction ----
    (
        "Onboarding",
        "Deck extract — PDF",
        "extract_text",
        "pypdf parser with per-page tagging + corrupt-PDF tolerance",
    ),
    (
        "Onboarding",
        "Deck extract — PPTX",
        "python-pptx",
        "Per-slide tagging; legacy .ppt refusal warning",
    ),
    (
        "Onboarding",
        "Production-mode stub refusal (P1 safety)",
        "_deck_stub_response",
        "Refuses to return 'Stub Co' when ANTHROPIC_API_KEY missing in prod",
    ),
    (
        "Onboarding",
        "Init wizard scaffolding",
        "init_wizard",
        "Interactive creation of company.yaml / sources / examples",
    ),
    # ---- CRM (Attio) ----
    (
        "CRM",
        "Attio activity poller (B6, real)",
        "list_activities_since",
        "Real HTTP to Attio /tasks; only Attio surface actually fetching",
    ),
    (
        "CRM",
        "Attio pipeline-updates poller (stubbed)",
        "list_pipeline_updates_since",
        "Returns [] in production — needs per-tenant schema mapping",
    ),
    (
        "CRM",
        "Attio investors poller (stubbed)",
        "list_investors",
        "Returns [] in production — same reason",
    ),
    (
        "CRM",
        "Outbound Stage 8 sync",
        "find_partner_record",
        "Email → linkedin_url → name+company match cascade against Attio",
    ),
    (
        "CRM",
        "Encrypted credential storage (Fernet)",
        "decrypt_api_key",
        "CRM_ENCRYPTION_KEY-bound Fernet symmetric encryption at rest",
    ),
    # ---- Gmail integration ----
    (
        "Gmail",
        "Gmail OAuth bootstrap",
        "gmail_bootstrap",
        "Per-user token persisted under workspace/secrets/",
    ),
    (
        "Gmail",
        "Gmail sent polling",
        "poll_gmail_sent_for_workspace",
        "Reads Sent box → outreach_events; HWM-resumable",
    ),
    (
        "Gmail",
        "Gmail reply polling",
        "poll_gmail_replies_for_workspace",
        "Reads Inbox → outreach_events (event_type='replied')",
    ),
    (
        "Gmail",
        "Reply classifier (heuristic + LLM fallback)",
        "classify_reply",
        "Cheap heuristic first; Claude only when 'unclear'",
    ),
    (
        "Gmail",
        "Gmail draft push (operator sends manually)",
        "create_gmail_drafts",
        "scripts/create_gmail_drafts.py creates draft objects in operator's account",
    ),
    # ---- Discovery / capture ----
    (
        "Discovery",
        "Discovery pool find_matches",
        "find_matches",
        "Ranked global pool reads, filtered by tenant's known partners",
    ),
    (
        "Discovery",
        "Discovery claim",
        "claim_investor",
        "Upsert fund + partner; stamp claimed_from_global_id",
    ),
    (
        "Discovery",
        "Per-tenant discovery opt-in (default OFF)",
        "_read_discovery_opt_in",
        "Operator-level kill switch INVESTORS_GLOBAL_DISABLED also gates",
    ),
    (
        "Capture",
        "QR capture endpoint",
        "capture_investor",
        "POST /investors/capture with LinkedIn URL dedup + collision handling",
    ),
    (
        "Capture",
        "LinkedIn URL normalization",
        "_normalize_linkedin_url",
        "Strip scheme/www/trailing slash/query for canonical dedup",
    ),
    (
        "Capture",
        "Partner-id collision suffix allocator",
        "_allocate_unique_partner_id",
        "Two different people same firm+name slug → -2, -3, ... suffix",
    ),
    # ---- Auth & multi-tenant ----
    (
        "Auth",
        "Supabase JWT verification",
        "_verify_supabase_jwt",
        "HS256 with SUPABASE_JWT_SECRET",
    ),
    (
        "Auth",
        "Legacy API_KEY fallback",
        "_is_api_key_fallback_enabled",
        "AUTH_ALLOW_API_KEY_FALLBACK gate during cutover",
    ),
    (
        "Auth",
        "Per-user workspace routing (contextvar)",
        "_CURRENT_USER_ID_VAR",
        "Async middleware stamps per-request; _engine_and_ws() reads",
    ),
    # ---- Operations / migration ----
    (
        "Operations",
        "_sync_columns_with_metadata (auto-migrate)",
        "_sync_columns_with_metadata",
        "ALTER TABLE ADD COLUMN with DEFAULT clause (audit-fix)",
    ),
    (
        "Operations",
        "Migration registry",
        "MIGRATIONS",
        "m001..m004; applied_migration_ids tracks applied",
    ),
    (
        "Operations",
        "Send-pace setting + hard daily cap enforcement",
        "_read_send_pace",
        "1-20 clamped; counted against outreach_events sent today",
    ),
    (
        "Operations",
        "Snooze + future-ISO parser",
        "parse_future_iso_naive_utc",
        "Shared parser; tz-naive UTC end-to-end",
    ),
    # ---- Per-channel ----
    (
        "Channels",
        "Per-partner channel preference",
        "channel_pref",
        "email / linkedin / both — column persisted",
    ),
    (
        "Channels",
        "LinkedIn mark-sent (FR-7)",
        "mark_draft_sent",
        "Operator-paste flow; logs outreach_events(source='app', channel='linkedin')",
    ),
    (
        "Channels",
        "Mark-sent clear (revert)",
        "clear_draft_sent",
        "Bypasses state machine (sent-is-terminal); writes audit row",
    ),
]


def main() -> None:
    tests_dir = REPO / "tests"

    # Group routes by tag (FastAPI's tag system) or by first path segment.
    rows = []
    for route in app.routes:
        if not hasattr(route, "methods"):
            continue
        for m in route.methods:
            if m == "HEAD":
                continue
            path = route.path
            tags = getattr(route, "tags", []) or []
            handler = getattr(route, "endpoint", None)
            handler_name = getattr(handler, "__name__", "<?>")
            handler_module = getattr(handler, "__module__", "<?>")
            tests = grep_tests_for_path(tests_dir, path, handler_name)
            rows.append({
                "method": m,
                "path": path,
                "tag": tags[0] if tags else "(untagged)",
                "handler": f"{handler_module}.{handler_name}",
                "tests": tests,
            })

    rows.sort(key=lambda r: (r["tag"], r["path"], r["method"]))

    # Group by tag.
    by_tag: dict[str, list] = {}
    for r in rows:
        by_tag.setdefault(r["tag"], []).append(r)

    out = ["# Feature Catalog (HTTP API surface)\n"]
    out.append(f"_Generated from `web.api.app`. {len(rows)} HTTP endpoints across {len(by_tag)} tags._\n")
    out.append("Each row maps an endpoint to the test file(s) that mention its path. **No test files** under `tests/` ⇒ flagged as gap.\n\n")
    total_covered = sum(1 for r in rows if r["tests"])
    out.append(f"**Coverage at endpoint granularity: {total_covered}/{len(rows)} endpoints have at least one test file mentioning their path.**\n\n")
    for tag in sorted(by_tag.keys()):
        tag_rows = by_tag[tag]
        out.append(f"## {tag} ({len(tag_rows)})\n")
        out.append("| Method | Path | Handler | Tests |")
        out.append("|---|---|---|---|")
        for r in tag_rows:
            tests_str = ", ".join(r["tests"][:3])
            if len(r["tests"]) > 3:
                tests_str += f" (+{len(r['tests']) - 3})"
            if not tests_str:
                tests_str = "**❌ no test mentions this path**"
            out.append(
                f"| `{r['method']}` | `{r['path']}` | `{r['handler']}` | {tests_str} |"
            )
        out.append("")

    # Non-HTTP surfaces.
    out.append("\n# Feature Catalog (non-HTTP surfaces)\n")
    out.append(
        "Subsystems, pipeline stages, background hooks, and internal "
        "modules. Each entry maps to test files that mention its symbol "
        "(function or module name).\n"
    )
    by_area: dict[str, list[tuple[str, str, str]]] = {}
    for area, name, symbol, purpose in _NON_HTTP_SURFACES:
        by_area.setdefault(area, []).append((name, symbol, purpose))
    nh_covered = 0
    nh_total = 0
    for area in sorted(by_area.keys()):
        items = by_area[area]
        out.append(f"\n## {area} ({len(items)})\n")
        out.append("| Feature | Symbol | Tests | Purpose |")
        out.append("|---|---|---|---|")
        for name, symbol, purpose in items:
            tests = grep_tests_for_symbol(tests_dir, symbol)
            nh_total += 1
            if tests:
                nh_covered += 1
                tstr = ", ".join(tests[:3])
                if len(tests) > 3:
                    tstr += f" (+{len(tests) - 3})"
            else:
                tstr = "**❌ no test mentions this symbol**"
            out.append(f"| {name} | `{symbol}` | {tstr} | {purpose} |")

    out.append("")
    out.append("---")
    out.append("")
    out.append("## Coverage summary")
    out.append("")
    out.append(f"- **HTTP endpoints**: {total_covered}/{len(rows)} have at least one test mention")
    out.append(f"- **Non-HTTP surfaces**: {nh_covered}/{nh_total} have at least one test mention")
    out.append("")
    out.append(
        "_'Test mention' = grep against `tests/` finds the path or symbol. "
        "It's a low-bar coverage signal: a test mentioning a feature doesn't "
        "guarantee correctness. Use this as a starting point for verification, "
        "not the final answer._"
    )

    target = REPO / "docs" / "FEATURE_CATALOG.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(out), encoding="utf-8")
    print(
        f"Wrote {target} -- {len(rows)} HTTP routes ({total_covered} covered) "
        f"+ {nh_total} non-HTTP surfaces ({nh_covered} covered)"
    )


if __name__ == "__main__":
    main()
