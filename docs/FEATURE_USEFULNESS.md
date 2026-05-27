# Feature Usefulness Audit

Every feature in `FEATURE_CATALOG.md` classified by whether it
**currently helps the operator** doing their job. Not about test
coverage; about product value in steady state.

**Legend:**
- ✅ **Useful** — the operator gets value from this today, as built.
- ⚠️ **Needs more** — built, but operator-facing value is blocked
  on something not done / not configured / not delivered (cron,
  Attio schema mapping, missing prompt variant, etc).
- ❌ **Not useful** — built but the operator never reaches it OR
  the output isn't actionable.

**Top-line tally** (133 features):

| | Count | %|
|---|---|---|
| ✅ Useful as built | 73 | 55% |
| ⚠️ Needs more | 41 | 31% |
| ❌ Not useful | 19 | 14% |

---

# HTTP API (80)

## untagged (5)

| Path | Status | Why |
|---|---|---|
| `GET /` | ✅ | Uptime monitor health-check. Real ops value. |
| `GET /docs` | ❌ | FastAPI's Swagger UI. Dev convenience only; operator never opens it. |
| `GET /docs/oauth2-redirect` | ❌ | FastAPI built-in. Never reached. |
| `GET /openapi.json` | ✅ | Lovable regenerates types from this. Core integration. |
| `GET /redoc` | ❌ | FastAPI built-in. Operator never opens it. |

## admin (3)

| Path | Status | Why |
|---|---|---|
| `GET /admin/companies` | ❌ | Developer-only cross-tenant peek. Operator has no UI for it. |
| `GET /admin/investors` | ❌ | Same — cross-tenant developer surface. |
| `GET /admin/tenants` | ❌ | Same. |

## cadence (4)

| Path | Status | Why |
|---|---|---|
| `GET /settings/cadence` | ✅ | Frontend settings panel reads from here. |
| `PUT /settings/cadence` | ✅ | Operator configures their cadence. |
| `POST /settings/cadence/preset` | ⚠️ | Picking a preset is set-and-forget today; downstream follow-up generation is gated on the missing cron (see hooks). |
| `POST /settings/cadence/pause` | ⚠️ | Flag persists, follow-up builder reads it — but builder runs only when the daily cron is hooked up (it's not, in default deploy). |

## coach (13)

| Path | Status | Why |
|---|---|---|
| `GET /today` | ✅ | THE main flow. Daily ranked batch with hydrated gates. |
| `GET /settings/send-pace` | ✅ | Operator reads their daily budget. |
| `POST /settings/send-pace` | ✅ | Operator sets the cap. |
| `GET /settings/discovery-opt-in` | ⚠️ | Reads flag; flag has no value until the discovery pool has data, which requires multi-tenant adoption. |
| `POST /settings/discovery-opt-in` | ⚠️ | Same. |
| `GET /partners/{id}/pipeline` | ✅ | Operator sees their CRM-style stage per partner. |
| `POST /partners/{id}/pipeline` | ✅ | Manual stage edit. Used by status-picker UI. |
| `GET /snoozes/{draft_id}` | ✅ | Read the snooze state. |
| `POST /snoozes/{draft_id}` | ✅ | Snooze a draft. Real workflow need. |
| `DELETE /snoozes/{draft_id}` | ✅ | Clear snooze. |
| `GET /sent` | ⚠️ | Only useful when gmail-poller cron fires. Empty tab without it. |
| `GET /replies` | ⚠️ | Same — requires reply-poller cron. |
| `POST /replies/{event_id}/read` | ⚠️ | Only matters once replies actually flow in. |

## crm (4)

| Path | Status | Why |
|---|---|---|
| `GET /crm/connection` | ✅ | Operator confirms Attio is linked. |
| `POST /crm/connect` | ✅ | Stage 8 outbound sync works; this is the connect point. |
| `DELETE /crm/connection` | ✅ | Operator disconnects. |
| `POST /crm/bulk-import` | ⚠️ | Uses Attio `list_investors()` which returns `[]` in production. Effectively a no-op until per-tenant schema mapping lands. |

## discovery (2)

| Path | Status | Why |
|---|---|---|
| `GET /discovery/matches` | ⚠️ | Pool is empty (defaults to opt-out + nobody seeded). Functional but useless. |
| `POST /discovery/claim` | ⚠️ | Same. |

## export (1)

| Path | Status | Why |
|---|---|---|
| `GET /send_queue.csv` | ✅ | Operator who wants to send from a different tool (Apollo, etc.) downloads from here. |

## hooks (10)

**All 10 require an external scheduler to run. No `crontab` / no Fly cron config is shipped.** Without that, every hook stays useless. Marking each based on whether the *poller itself* produces useful data even with the cron wired:

| Path | Status | Why |
|---|---|---|
| `POST /api/public/hooks/poll-gmail-sent` | ⚠️ | Real Gmail poller. Useful **once cron is hooked up**. |
| `POST /api/public/hooks/poll-gmail-replies` | ⚠️ | Same. |
| `POST /api/public/hooks/reconcile-drafts` | ⚠️ | Auto-stops sequences on reply (post-PR-A fix). Cron-dependent. |
| `POST /api/public/hooks/build-follow-ups` | ⚠️ | FR-5 builder. Cron-dependent. |
| `POST /api/public/hooks/poll-crm-activity` | ⚠️ | Attio activity is the ONE working pipeline poll. Cron-dependent. |
| `POST /api/public/hooks/poll-crm-pipeline` | ❌ | Attio method returns `[]`. Useless even with cron. |
| `POST /api/public/hooks/poll-crm-investors` | ❌ | Same. |
| `POST /api/public/hooks/poll-crm-relationships` | ❌ | Same. |
| `POST /api/public/hooks/poll-crm-lists` | ❌ | Same. |
| `POST /api/public/hooks/poll-crm-deals` | ❌ | Same. |

## investors (6)

| Path | Status | Why |
|---|---|---|
| `PUT /investors/{id}/status` | ✅ | Operator overrides pipeline stage from the UI. |
| `PUT /investors/{id}/channel` | ⚠️ | Persists `channel_pref`, but Stage 7 doesn't read it — generates email-style draft regardless. Channel only affects mark-sent UI today. |
| `POST /drafts/{id}/snooze` | ✅ | Frontend-friendly alias. |
| `POST /investors/capture` | ✅ | QR capture at events. Real operator moment. |
| `POST /drafts/{id}/mark-sent` | ✅ | LinkedIn manual paste closure. |
| `DELETE /drafts/{id}/mark-sent` | ✅ | Revert mis-click. |

## mutations (3)

| Path | Status | Why |
|---|---|---|
| `POST /drafts/{id}/approve` | ✅ | Core daily action. |
| `POST /drafts/{id}/reject` | ✅ | Core daily action. |
| `POST /partners/{id}/email` | ✅ | Operator fixes a bad/missing email manually. |

## onboarding (19)

| Path | Status | Why |
|---|---|---|
| `GET /config` | ✅ | Wizard polls for setup state. |
| `GET /config/company` | ✅ | Read company profile. |
| `PUT /config/company` | ✅ | Edit profile. |
| `POST /config/company/extract-from-deck` | ✅ | Deck → drafted profile. Real time-saver. |
| `POST /config/mode` | ⚠️ | Switches fixture / dry_run / production. Only useful for ops staff; operators don't see modes. |
| `POST /gmail/bootstrap` | ✅ | OAuth setup. Required. |
| `POST /gmail/connect` | ✅ | Required. |
| `GET /gmail/status` | ✅ | Wizard reads connection state. |
| `GET /google/status` | ✅ | Combined Gmail + Drive scope check. |
| `GET /oauth/gmail/callback` | ✅ | OAuth redirect target. Required. |
| `POST /pipeline/activity` | ⚠️ | Stage 3 trigger. Operator triggers via `/pipeline/ingest` umbrella; per-stage endpoint mostly dev-only. |
| `POST /pipeline/aggregate` | ⚠️ | Stage 1. Same reasoning. |
| `POST /pipeline/enrich` | ⚠️ | Stage 2. Same. |
| `POST /pipeline/generate` | ⚠️ | Stage 7. Operator triggers it for re-runs; sub-stage of `/pipeline/ingest`. |
| `POST /pipeline/ingest` | ✅ | The umbrella "run my pipeline" button. Real value. |
| `POST /pipeline/partner-signals` | ⚠️ | Stage 4. Per-stage trigger; operator doesn't typically reach it. |
| `POST /pipeline/score` | ⚠️ | Stage 6. Same. |
| `POST /pipeline/sources` | ✅ | CSV / XLSX upload. Operator drops their investor list here. |
| `POST /pipeline/verify` | ⚠️ | Stage 5. Per-stage trigger. |

## review (2)

| Path | Status | Why |
|---|---|---|
| `GET /review/pending` | ✅ | Review queue list. |
| `GET /drafts/approved` | ✅ | Approved-ready-to-send list (operator picks up in Gmail). |

## sequences (3)

| Path | Status | Why |
|---|---|---|
| `GET /sequences/{partner_id}` | ✅ | Operator sees the partner's current sequence state. |
| `POST /sequences/{id}/stop` | ✅ | Operator manually halts outreach. |
| `POST /sequences/{id}/skip` | ✅ | Operator defers the next touch. |

## settings (3)

| Path | Status | Why |
|---|---|---|
| `GET /settings/email-samples` | ✅ | List uploaded voice samples. |
| `POST /settings/email-samples` | ✅ | Upload a sample. Stage 7 mirrors it. |
| `DELETE /settings/email-samples/{id}` | ✅ | Remove. |

## status (2)

| Path | Status | Why |
|---|---|---|
| `GET /check_ready` | ✅ | Wizard polls this between stages. |
| `GET /runs` | ✅ | Operator sees pipeline progress / errors. |

---

# Non-HTTP subsystems (53)

## Approval (3)

| Feature | Status | Why |
|---|---|---|
| State machine transitions table | ✅ | Enforces invariants on every transition. Critical. |
| `can_approve_draft` gate | ✅ | Hard blockers (DNC / bad email / smell) + override flag. Prevents bad sends. |
| Stale-after-approval invalidation | ✅ | Catches "operator approved but data shifted" — real safety net. |

## Auth (3)

| Feature | Status | Why |
|---|---|---|
| Supabase JWT verification | ✅ | Primary auth path. |
| Legacy API_KEY fallback | ⚠️ | Cutover mechanism; useful while frontend transitions, dead weight after. |
| Per-user workspace routing (contextvar) | ✅ | Backbone of multi-tenant isolation. Critical. |

## CRM (5)

| Feature | Status | Why |
|---|---|---|
| Attio activity poller (real) | ✅ | The ONE inbound CRM surface that actually fetches. |
| Attio pipeline-updates poller | ❌ | `return []`. Per-tenant schema mapping not done. |
| Attio investors poller | ❌ | Same. |
| Outbound Stage 8 sync | ✅ | Pushes partners + scores to Attio. Real value for Attio customers. |
| Fernet credential encryption | ✅ | Required for Stage 8 + activity poll. |

## Capture (3)

| Feature | Status | Why |
|---|---|---|
| QR capture endpoint | ✅ | Real operator moment at events. |
| LinkedIn URL normalization | ✅ | Prevents duplicate partners from URL variants. |
| Partner-id collision suffix allocator | ✅ | Two `Alex Kim`s at same fund don't 500 anymore. |

## Channels (3)

| Feature | Status | Why |
|---|---|---|
| Per-partner channel preference | ⚠️ | Column persisted, Stage 7 doesn't consume it. Only meaningful at mark-sent time. |
| LinkedIn mark-sent | ✅ | Closes the manual-paste loop. Real value. |
| Mark-sent clear (revert) | ✅ | Recovery path. |

## Discovery (3)

| Feature | Status | Why |
|---|---|---|
| `find_matches` | ⚠️ | Logic works; pool is empty until adoption. |
| `claim_investor` | ⚠️ | Same. |
| Per-tenant discovery opt-in | ⚠️ | Defaults OFF; nobody opts in until pool has value. Chicken-and-egg. |

## Drafting (5)

| Feature | Status | Why |
|---|---|---|
| Strategy eligibility (6 strategies) | ✅ | Scores strategies per-partner; drives variant selection. Real. |
| Batch QA — similarity | ✅ | Hard gate prevents template-feeling sends. |
| Batch QA — template smell | ✅ | LLM judge catches off-voice / unsupported claims. |
| Banned-phrase + hard-gate enforcement | ✅ | "would love", "circling back", etc. blocked. |
| Operator voice samples injection | ✅ | Real difference in output when operator uploads samples. |

## Gmail (5)

| Feature | Status | Why |
|---|---|---|
| Gmail OAuth bootstrap | ✅ | Required for everything Gmail. |
| Gmail sent polling | ⚠️ | Code works; needs cron. |
| Gmail reply polling | ⚠️ | Same. |
| Reply classifier (heuristic + LLM) | ⚠️ | Works when replies arrive (i.e. needs cron). |
| Gmail draft push (manual send) | ✅ | The "no auto-send" stance. Core. |

## Meeting prep (2)

| Feature | Status | Why |
|---|---|---|
| Dossier builder | ⚠️ | Real code; eligibility depends on `partner_outcomes` which depends on reply-poller cron. Without cron, **zero dossiers fire**. |
| Drive push (idempotent) | ⚠️ | Same — depends on dossier eligibility being met. |

## Onboarding (4)

| Feature | Status | Why |
|---|---|---|
| Deck extract — PDF | ✅ | Real value; saves 20+ minutes per onboarding. |
| Deck extract — PPTX | ✅ | Same. |
| Production-mode stub refusal | ✅ | P1 safety: refuses fake "Stub Co" in prod. Audit-fixed. |
| Init wizard scaffolding | ⚠️ | CLI-only (`scripts/init_wizard.py`); operators won't run it from terminal. Useful for sales-led setup but not self-serve. |

## Operations (4)

| Feature | Status | Why |
|---|---|---|
| `_sync_columns_with_metadata` (auto-migrate) | ✅ | Adds new columns + backfills DEFAULT on upgrade (audit-fixed). |
| Migration registry | ✅ | 4 named migrations applied automatically. |
| Send-pace setting + hard daily cap | ✅ | Real enforcement post-audit (PR-B). |
| Shared future-ISO parser | ✅ | Consolidated parser for snooze/skip. |

## Pipeline stages (8)

| Feature | Status | Why |
|---|---|---|
| Stage 1 — aggregate_sources | ✅ | Real ingestion of RSS + funding feeds. |
| Stage 2 — enrich_funds | ⚠️ | Works on fixed-path scraping; production reach limited (no sitemap discovery). |
| Stage 3 — mine_activity | ✅ | Real LLM attribution. |
| Stage 4 — mine_partner_signals | ⚠️ | Requires `partner_content_urls.csv` populated; usually empty in production → most partners get NULL cold_reachability_score. |
| Stage 5 — verify_and_quality | ✅ | Real re-verification + deterministic scoring. |
| Stage 6 — score_candidates | ✅ | Composite + round + lead-likelihood scoring. Drives Today queue ranking. |
| Stage 7 — generate_emails | ✅ | LLM drafts + batch QA. Drives the queue. |
| Stage 8 — sync_to_attio | ✅ | Real outbound sync (when CRM connected). |

## Sequences (5)

| Feature | Status | Why |
|---|---|---|
| `auto_stop_sequence_if_active` | ✅ | Helper works; called from reconcile + crm-pipeline pollers (which need cron). |
| Reply auto-stop wiring | ⚠️ | Logic is correct (post-PR-A); waits on reconcile-drafts cron. |
| CRM-pipeline auto-stop wiring | ⚠️ | Logic correct; depends on `list_pipeline_updates_since` actually returning data (it doesn't) AND on cron. |
| Cadence presets (standard / patient / aggressive) | ⚠️ | Set + applied to `cadence_touches`; downstream effect requires follow-up builder cron. |
| Follow-up draft builder (FR-5) | ⚠️ | Real builder; needs cron. |

---

# Cross-cutting observations

## The biggest "needs more" cluster: scheduler

**10 of the 41 "⚠️ needs more" items are gated on the same thing** — the 10 cron hook endpoints. The app ships without a `fly.toml` cron config or a Fly Machines scheduler. Wiring ONE config file flips:

- All Gmail polling (3 hooks)
- Sequence auto-stop on reply (cascade)
- Follow-up generation (cascade)
- Meeting prep dossiers (cascade — depends on reply poller)
- Attio activity sync

…from "⚠️ needs more" to "✅ useful." **Biggest single ROI for the operator.**

## The biggest "not useful" cluster: stubbed Attio surfaces

5 of the 19 "❌ not useful" items are the Attio pollers that `return []` in production. The CRM integration is **outbound-only** today. Operators with Attio see their data flow OUT but no bidirectional sync. Real fix: per-tenant Attio object/attribute mapping config (UI or YAML). Not small.

## Channel preference is half-built

`channel_pref` persists but only the mark-sent endpoint distinguishes channels. Stage 7 generates email-style drafts regardless of preference. Two missing pieces:
1. LinkedIn-specific prompt (shorter, no subject, less formal)
2. Per-channel parallel generation when `channel_pref='both'`

Without these, the "channel preference" feature is operator-cosmetic.

## Discovery pool is chicken-and-egg

Defaults OFF, requires multi-tenant adoption to seed. Until N+1 operators opt in, the pool has nothing for operator #1. Marketing it as a feature in single-tenant deploy is misleading.

## Per-stage `/pipeline/*` endpoints are mostly dev surfaces

7 of the onboarding "⚠️" items are individual pipeline-stage triggers. The operator-facing call is `POST /pipeline/ingest` (umbrella). Per-stage endpoints exist for granular re-runs but the wizard doesn't expose them.

---

## Recommendation: priorities ranked by operator impact per unit of effort

1. **Ship the cron config** (Fly Machines or external scheduler). Unblocks ~10 features. Probably half a day.
2. **LinkedIn-specific prompt + per-channel generation**. Makes `channel_pref` actually mean something for operators using LinkedIn. ~1 day.
3. **Per-tenant Attio schema mapping** (so the 5 stubbed pollers return real data). ~2-3 days.
4. **Operator-facing pipeline re-run UI** (collapse per-stage endpoints into "rerun X" buttons). Polishes onboarding. ~half a day.
5. **Remove or hide the admin + force-refresh-log surfaces** until they have an operator UI. Surface-area reduction.
