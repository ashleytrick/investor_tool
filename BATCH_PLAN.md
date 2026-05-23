# Inventory Reconciliation + Batch Plan

Source: items 281–1200 from the rolling code-review inventory (1001–1100 not provided). Reconciled against working tree at commit `806940c` (post Batch 8).

## Legend
- **DONE** — verified fixed in tree
- **PARTIAL** — partially addressed; remaining work folded into a later batch
- **OPEN** — concrete, valuable, actionable; assigned to a batch
- **DEFER** — speculative / requires design decision the operator should make / not yet in scope
- **WONTFIX** — not applicable, redundant, or unfounded

---

## Already done (no action needed)

| # | Item | Where |
|---|---|---|
| 281 | verify_attio_schema exit 2 on missing key | Batch 8 |
| 285 | matching_attributes preflight | Batch 4 |
| 286 | manual_override --clear nonexistence | Finding 49 (pre-batch) |
| 293 | apply --all-above exit 2 on failures | Batch 8 |
| 311 | JSON first/last brace tolerance | Batch 7 (crash path) |
| 340 | Stage 1 all-sources-fail exit 2 | already in code (line 146) |
| 384 | Stage 8 person_conflict counts as failed | Findings 37/44 |
| 426/427 | Stub axis scoring ignores signal_direction | Batch 8 |
| 430/431/432 | Stage 7 eligibility filters positive direction | Batch 8 |
| 437/438/439 | fund.kill_signals in major_kill | Batch 8 |
| 440 | KILL_SIGNALS placeholder filled | already in code (line 482) |
| 451/452/453 | Example files injected into prompt | Batch 8 |
| 489–500 | Status metric gaps — addressed by `status.py` already covering most; remaining specifics → Batch 12 |
| 503–507 | FK enforcement at DB level | Batch 6 |
| 521/525/526 | outcomes.source column + exclusion | Batch 8 |
| 545/546 | SQLite journal/busy_timeout — not configured but WAL is on; defer |
| 774–783 | Hot-path indexes | Batch 6 |
| 905/906 | Stale summary/score clearing tested | Batch 6 |
| 914 | Negative-signal score test | Batch 8 |
| 952/953 | Stage 7 batch QA hard failure | Stage 7 gate commit |
| 1000 | Fixture-outcome contamination test | Batch 8 |

---

## Batches (~10–15 items each, themed)

### Batch 9 — Production-safety guards (placeholders, .example domains)
Operator scaffolds from fixture → accidentally runs real outreach.
- **533**: block `{PLACEHOLDER}` strings in generated CSV outputs
- **534**: block `{PLACEHOLDER}` in any email/deck/follow-up body persisted
- **535**: block `cal.example` scheduling links in production CSV
- **536**: block `.example` partner email addresses before Gmail draft creation
- **532**: block `.example` fund domains in Stage 8 sync payloads
- **446**: refuse `ready_to_send` when scheduling link is a placeholder
- **445**: refuse `ready_to_send` when founder_email is missing/placeholder
- **531**: block Stage 8 sync of partners whose fund_domain ends in `.example`
- **1137**: block `ready_to_send` with no contact method (no email on partner)
- **1136**: include partner email in CSV when present

### Batch 10 — Schema tightening (Pydantic validators)
Reject malformed LLM outputs at the schema layer, not downstream.
- **595**: deal_attribution.round_size_usd ≥ 0
- **596/597**: deal_attribution.company / partner / fund non-empty
- **598**: deal_attribution.announcement_date not future-dated
- **599**: deal_attribution.round_type bounded enum
- **600**: fund_enrichment.stated_stage_focus bounded enum
- **602/603/604**: partner_signal source_type / confidence / signal_direction → Literal
- **605**: partner_signal quote_date not future-dated
- **606**: partner_signal quoted_text max length
- **610**: email subject cannot be a question
- **611**: email subject max-5-words enforcement
- **613/614**: preemption_line ↔ objection_preempted consistency
- **615/616**: candidate_score supporting_signal_ids existence + ownership (post-validate)

### Batch 11 — Stage hygiene fixes
- **348**: Stage 4 clears `cold_reachability_partial_score` when no fresh content
- **351**: Stage 5 clears `signal_quality_score` when verification fails
- **352**: Stage 5 clears `quality_reasoning` on unverified transition
- **357**: Stage 6 returns non-zero when any partner failed
- **366**: Stage 7 doesn't delete drafts for partners later skipped (stub miss / no strategy)
- **412/413**: Stage 2 doesn't overwrite richer prior enrichment with poorer new values
- **477/478/479/480**: Stage 8 selects latest recommended draft by draft_id, not iteration order
- **383**: Stage 8 update_record skipped + logged when payload is empty
- **434/435**: CSV top_signals sorts/filters by direction; flag anti-fit warning
- **436**: kill_signal_summary includes negative thesis signals → already done in Batch 8

### Batch 12 — Stage 8 audit + drafts pushed-at timestamps
- **379**: Stage 8 sets email_drafts.pushed_to_attio_at on success
- **380**: Stage 8 sets followup_drafts.pushed_to_attio_at
- **381**: Stage 8 sets deck_request_responses.pushed_to_attio_at
- **638**: attio_sync_log persists request payload (redacted)
- **641**: preserved-fields removed list logged in run.note (already prints, also persist)
- **385**: Stage 8 always closes Attio client (try/finally)
- **496**: status.py shows Attio sync failures separately
- **985**: test for pushed_to_attio_at being set

### Batch 13 — Doctor command + invariant checks
A single `scripts/doctor.py` consolidating DB-level integrity validation.
- **501**: new `scripts/doctor.py`
- **502**: every recommended partner has a recommended draft
- **508**: every score axis_id exists in axes.yaml
- **509**: no duplicate unapproved suggestions for the same axis
- **510**: no out-of-range scores in DB (composite ∈ [0,10], axis ∈ [0,10])
- **511**: no future-dated signals/deals/outcomes
- **513**: no recommended drafts with empty subject/body
- **515**: no orphaned source_snapshots (zero signals/funds reference them)
- **516**: no unverified signals with non-null quality
- **517**: no verified signals with null quality
- **520**: no warm-path partners marked ready_to_send (Stage 7 should route, but double-check)
- **518/519**: stale employment / fund-active diagnostics

### Batch 14 — Workspace + path safety + small CLIs
- **300**: detect typo in `INVESTOR_WORKSPACE` (refuse if it would create data/exports under repo root)
- **302**: Workspace.name disambiguation when basename collides
- **303**: db_url URL-escapes path
- **304/305**: friendlier YAML diagnostics
- **307/308**: `print_banner` accurate Attio "ready" state + model display
- **794/797**: ensure `.gitignore` in client workspace covers DB/exports/raw
- **683**: `set_employment_status.py left_fund` CLI
- **685**: `set_fund_inactive.py` CLI
- **686**: `set_partner_linkedin.py` CLI
- **688/689**: `list_missing_fields.py` for partners + funds
- **691**: `list_blocked_recommendations.py`

### Batch 15 — Manual override polish + audit
- **287**: `--clear` should accept `--score-only` / `--rec-only` / `--warm-only` granularity
- **288**: manual_override_reason keyed by type (score vs rec vs warm) so reasons don't collide
- **289/290**: warm-path reason persisted on partner row; contact required by default
- **291**: `--list` shows recommended/score/send_now_priority (mostly there, verify completeness)
- **292**: `apply --list` doesn't pollute runs (mark as `note` not `processed`)
- **296**: apply_axis_suggestion records `approved_by` (operator name from env)
- **298**: backup rotation logs which backups deleted

### Batch 16 — Concrete tests for existing behavior (high-leverage subset of 801–1000)
Pick the tests that pin behavior we've already shipped but lack coverage for.
- **828**: assert no `{X}` placeholders remain after build_live_prompt
- **829**: live prompt contains all example file contents
- **834/927**: fund kill_signals blocks recommendation
- **841/842**: manual_override --recommend yes / no
- **887/888**: Stage 4 stale signal/reachability clearing (after Batch 11)
- **895**: Stage 5 verified=False clears quality (after Batch 11)
- **907**: Stage 6 partner without fund (no_fund branch already tested? confirm)
- **910/911**: Stage 6 unknown axis IDs are dropped/warned
- **919–923**: Stage 6 check-size edge cases (commas, malformed, min>max, missing config)
- **934**: Stage 6 process exits non-zero when partner exceptions (after Batch 11 #357)
- **940/941/942**: Stage 7 missing-recommended / empty body / empty deck schema validation
- **957/958**: forbidden phrases in subject/follow-up/deck

### Batch 17 — Stage 7 dependency freshness (363–365)
Refuse to run Stage 7 when upstream stages are stale.
- **363**: Stage 7 refuses if no successful Stage 6 in last N hours
- **364**: Stage 7 refuses if Stage 5 hasn't run since Stage 4
- **365**: Stage 7 refuses if Stage 3 hasn't run since Stage 1/2
- **499/500**: status.py compares timestamps across stages, surfaces stale warnings
- **970**: test for stale Stage 6 dependency

### Batch 18 — Attio robustness + safety
- **653/654/655**: api_base allowlist; refuse non-`api.attio.com` unless explicit allow
- **658**: workspace_id verified against Attio API response on first connect
- **663/664/665**: idempotency / duplicate-create guards (best-effort: search by name+domain before create)
- **669**: `scripts/attio_dedupe.py` to detect duplicate Attio records
- **675/674**: enforce UNIQUE on funds.attio_record_id / partners.attio_record_id
- **988**: test for malicious api_base refusal

### Batch 19 — Outcome state machine (subset of 1101–1131)
- **1101/1102**: skip regen when outreach_status=sent/replied (per partner)
- **1103**: skip recommendation when meeting_booked=True
- **1119/1120**: outcome status enum (Literal) instead of free string
- **1122**: Stage 7 derives outreach_status considering prior outcome
- **1125/1126**: Stage 7 + Stage 8 honor local outcomes as suppression

### Batch 20 — Test backlog wave 2 (additional high-leverage from 801–1000)
- **815**: root .env vs workspace .env precedence
- **816**: empty process env override
- **819**: fixture mode without API key
- **821/822/823**: LLM retry paths (malformed JSON, schema invalid, final failure)
- **866/867/868**: Stage 3 future-date / duplicates / same-URL-updated
- **915**: Stage 6 invalid score confidence value
- **969**: Stage 7 top-N with null priority

---

## Items intentionally deferred (design decisions required)

| Category | Items | Reason |
|---|---|---|
| Concurrency / locks | 541–544, 547–552 | Workspace is single-operator; lockfile design needs operational input |
| Performance / parallelism | 553–558, 571 | Adds complexity; bottleneck not measured |
| Rate limiting scope | 559–566, 568 | Cross-process bucketing needs daemon design |
| Context window mgmt | 572–580 | Live LLM not exercised yet; data-driven decisions |
| Retention / encryption | 623, 784–792 | Compliance policy decision |
| Robots.txt / scraping ethics | 328, 329 | Policy decision |
| Send provider integration | 1111–1118, 1131–1140 | Out of scope; CSV → human → Gmail is the design |
| Bounce / unsubscribe / compliance | 1139–1143 | Out of scope without send integration |
| Cross-workspace dedupe | 1185–1195 | Multi-tenant design decision |
| Geography extraction | 580–584, 591–593 | Schema expansion that needs deliberate design |
| Provisional record creation | 741–745 | Stage 3 design choice; current behavior is "drop unmatched" intentionally |
| Stages 4/5 manual correction | 761–763 | Separate workstream |
| Pipeline state versioning | 401–406, 460–462 | Major design |
| Outreach review CSV roundtrip | 1144–1155 | Separate workstream |

---

## Items reconciled as WONTFIX

| # | Reason |
|---|---|
| 783 | False — indexes ARE declared since Batch 6 |
| 774–781 | All indexes done in Batch 6 |
| 367 | `batch_size` naming — column rename costly, doc-only |
| 615 (full) | "Signal IDs not belonging to partner" requires reaching back into per-partner state; partial-check only |
| 1158–1170 | Founder/intro features are net-new product surface |

---

## Execution order

Working through Batches 9 → 20 in order. Each batch: implement, smoke tests stay green, commit, push. Each commit ≤ ~15 file changes; new tests added for behavior changes.

If a batch reveals a deeper structural issue, document it here and move on.
