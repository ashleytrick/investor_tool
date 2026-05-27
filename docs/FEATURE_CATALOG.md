# Feature Catalog (HTTP API surface)

_Generated from `web.api.app`. 80 HTTP endpoints across 15 tags._

Each row maps an endpoint to the test file(s) that mention its path. **No test files** under `tests/` ⇒ flagged as gap.


**Coverage at endpoint granularity: 78/80 endpoints have at least one test file mentioning their path.**


## (untagged) (5)

| Method | Path | Handler | Tests |
|---|---|---|---|
| `GET` | `/` | `web.api.root` | test_admin.py, test_api.py, test_apollo_workflow.py (+100) |
| `GET` | `/docs` | `fastapi.applications.swagger_ui_html` | test_drive_sync.py |
| `GET` | `/docs/oauth2-redirect` | `fastapi.applications.swagger_ui_redirect` | **❌ no test mentions this path** |
| `GET` | `/openapi.json` | `fastapi.applications.openapi` | test_api.py, test_deck_extraction.py |
| `GET` | `/redoc` | `fastapi.applications.redoc_html` | **❌ no test mentions this path** |

## admin (3)

| Method | Path | Handler | Tests |
|---|---|---|---|
| `GET` | `/admin/companies` | `web.routers.admin.admin_companies` | test_admin.py, test_api.py, test_review_small_fixes.py |
| `GET` | `/admin/investors` | `web.routers.admin.admin_investors` | test_admin.py, test_api.py |
| `GET` | `/admin/tenants` | `web.routers.admin.admin_tenants` | test_admin.py, test_api.py, test_review_small_fixes.py |

## cadence (4)

| Method | Path | Handler | Tests |
|---|---|---|---|
| `GET` | `/settings/cadence` | `web.routers.cadence.get_cadence` | test_cadence_settings.py |
| `PUT` | `/settings/cadence` | `web.routers.cadence.put_cadence` | test_cadence_settings.py |
| `POST` | `/settings/cadence/pause` | `web.routers.cadence.pause_cadence` | test_cadence_settings.py |
| `POST` | `/settings/cadence/preset` | `web.routers.cadence.apply_preset` | test_cadence_settings.py |

## coach (13)

| Method | Path | Handler | Tests |
|---|---|---|---|
| `GET` | `/partners/{partner_id}/pipeline` | `web.routers.coach.get_pipeline` | test_api.py, test_authenticated_e2e.py, test_investor_endpoints.py (+2) |
| `POST` | `/partners/{partner_id}/pipeline` | `web.routers.coach.set_pipeline` | test_api.py, test_authenticated_e2e.py, test_investor_endpoints.py (+2) |
| `GET` | `/replies` | `web.routers.coach.get_replies` | test_api.py, test_authenticated_e2e.py, test_outreach_replies.py (+1) |
| `POST` | `/replies/{event_id}/read` | `web.routers.coach.mark_reply_as_read` | test_api.py, test_authenticated_e2e.py, test_outreach_replies.py (+1) |
| `GET` | `/sent` | `web.routers.coach.get_sent` | test_api.py, test_authenticated_e2e.py, test_outreach_sent.py (+1) |
| `GET` | `/settings/discovery-opt-in` | `web.routers.coach.get_discovery_opt_in` | test_api.py, test_authenticated_e2e.py, test_discovery_opt_in.py (+2) |
| `POST` | `/settings/discovery-opt-in` | `web.routers.coach.set_discovery_opt_in` | test_api.py, test_authenticated_e2e.py, test_discovery_opt_in.py (+2) |
| `GET` | `/settings/send-pace` | `web.routers.coach.get_send_pace` | test_api.py, test_authenticated_e2e.py, test_per_user_endpoint_coverage.py (+4) |
| `POST` | `/settings/send-pace` | `web.routers.coach.set_send_pace` | test_api.py, test_authenticated_e2e.py, test_per_user_endpoint_coverage.py (+4) |
| `DELETE` | `/snoozes/{draft_id}` | `web.routers.coach.clear_snooze` | test_api.py, test_authenticated_e2e.py, test_investor_endpoints.py (+3) |
| `GET` | `/snoozes/{draft_id}` | `web.routers.coach.get_snooze` | test_api.py, test_authenticated_e2e.py, test_investor_endpoints.py (+3) |
| `POST` | `/snoozes/{draft_id}` | `web.routers.coach.set_snooze` | test_api.py, test_authenticated_e2e.py, test_investor_endpoints.py (+3) |
| `GET` | `/today` | `web.routers.coach.get_today` | test_api.py, test_authenticated_e2e.py, test_followup_builder.py (+5) |

## crm (4)

| Method | Path | Handler | Tests |
|---|---|---|---|
| `POST` | `/crm/bulk-import` | `web.routers.crm.crm_bulk_import` | test_api.py, test_crm_b7_b8_b9.py |
| `POST` | `/crm/connect` | `web.routers.crm.crm_connect` | test_api.py, test_authenticated_e2e.py, test_crm_b7_b8_b9.py (+4) |
| `DELETE` | `/crm/connection` | `web.routers.crm.crm_disconnect` | test_api.py, test_authenticated_e2e.py, test_crm_connections.py (+1) |
| `GET` | `/crm/connection` | `web.routers.crm.get_crm_connections` | test_api.py, test_authenticated_e2e.py, test_crm_connections.py (+1) |

## discovery (2)

| Method | Path | Handler | Tests |
|---|---|---|---|
| `POST` | `/discovery/claim` | `web.api.discovery_claim` | test_api.py, test_discovery.py, test_investors_capture.py (+1) |
| `GET` | `/discovery/matches` | `web.api.discovery_matches` | test_api.py, test_discovery.py |

## export (1)

| Method | Path | Handler | Tests |
|---|---|---|---|
| `GET` | `/send_queue.csv` | `web.api.send_queue_csv` | test_api.py, test_approval_gates.py |

## hooks (10)

| Method | Path | Handler | Tests |
|---|---|---|---|
| `POST` | `/api/public/hooks/build-follow-ups` | `web.routers.hooks.hook_build_follow_ups` | test_followup_builder.py |
| `POST` | `/api/public/hooks/poll-crm-activity` | `web.routers.hooks.hook_poll_crm_activity` | test_api.py, test_crm_polling.py |
| `POST` | `/api/public/hooks/poll-crm-deals` | `web.routers.hooks.hook_poll_crm_deals` | test_api.py, test_crm_b7_b8_b9.py |
| `POST` | `/api/public/hooks/poll-crm-investors` | `web.routers.hooks.hook_poll_crm_investors` | test_api.py, test_crm_b7_b8_b9.py |
| `POST` | `/api/public/hooks/poll-crm-lists` | `web.routers.hooks.hook_poll_crm_lists` | test_api.py, test_crm_b7_b8_b9.py |
| `POST` | `/api/public/hooks/poll-crm-pipeline` | `web.routers.hooks.hook_poll_crm_pipeline` | test_api.py, test_crm_polling.py |
| `POST` | `/api/public/hooks/poll-crm-relationships` | `web.routers.hooks.hook_poll_crm_relationships` | test_api.py, test_crm_b7_b8_b9.py |
| `POST` | `/api/public/hooks/poll-gmail-replies` | `web.routers.hooks.hook_poll_gmail_replies` | test_api.py, test_outreach_replies.py |
| `POST` | `/api/public/hooks/poll-gmail-sent` | `web.routers.hooks.hook_poll_gmail_sent` | test_api.py, test_authenticated_e2e.py, test_outreach_sent.py |
| `POST` | `/api/public/hooks/reconcile-drafts` | `web.routers.hooks.hook_reconcile_drafts` | test_api.py, test_outreach_replies.py, test_sequence_auto_stop.py |

## investors (6)

| Method | Path | Handler | Tests |
|---|---|---|---|
| `DELETE` | `/drafts/{draft_id}/mark-sent` | `web.routers.investors.clear_draft_sent` | test_api.py, test_drafts_mark_sent.py, test_investor_endpoints.py |
| `POST` | `/drafts/{draft_id}/mark-sent` | `web.routers.investors.mark_draft_sent` | test_api.py, test_drafts_mark_sent.py, test_investor_endpoints.py |
| `POST` | `/drafts/{draft_id}/snooze` | `web.routers.investors.snooze_draft_alias` | test_api.py, test_drafts_mark_sent.py, test_investor_endpoints.py |
| `POST` | `/investors/capture` | `web.routers.investors.capture_investor` | test_investors_capture.py, test_sequences.py |
| `PUT` | `/investors/{partner_id}/channel` | `web.routers.investors.set_investor_channel` | test_admin.py, test_api.py, test_investor_endpoints.py (+2) |
| `PUT` | `/investors/{partner_id}/status` | `web.routers.investors.set_investor_status` | test_admin.py, test_api.py, test_investor_endpoints.py (+2) |

## mutations (3)

| Method | Path | Handler | Tests |
|---|---|---|---|
| `POST` | `/drafts/{draft_id}/approve` | `web.api.approve_draft` | test_api.py, test_apollo_workflow.py, test_approval_clis.py (+11) |
| `POST` | `/drafts/{draft_id}/reject` | `web.api.reject_draft` | test_api.py, test_approval_clis.py, test_approval_gates.py (+2) |
| `POST` | `/partners/{partner_id}/email` | `web.api.set_partner_email` | test_api.py, test_authenticated_e2e.py, test_investor_endpoints.py (+5) |

## onboarding (19)

| Method | Path | Handler | Tests |
|---|---|---|---|
| `GET` | `/config` | `web.api.get_config` | test_api.py, test_company_config.py, test_deck_extraction.py (+1) |
| `GET` | `/config/company` | `web.api.get_company` | test_api.py, test_deck_extraction.py, test_review_small_fixes.py |
| `PUT` | `/config/company` | `web.api.put_company` | test_api.py, test_deck_extraction.py, test_review_small_fixes.py |
| `POST` | `/config/company/extract-from-deck` | `web.api.extract_from_deck` | test_api.py, test_deck_extraction.py, test_review_small_fixes.py |
| `POST` | `/config/mode` | `web.api.set_mode` | test_api.py |
| `POST` | `/gmail/bootstrap` | `web.routers.google.gmail_bootstrap` | test_api.py, test_gmail_bootstrap.py |
| `POST` | `/gmail/connect` | `web.routers.google.gmail_connect` | test_api.py, test_gmail_bootstrap.py |
| `GET` | `/gmail/status` | `web.routers.google.gmail_status` | test_api.py |
| `GET` | `/google/status` | `web.routers.google.google_status` | test_api.py |
| `GET` | `/oauth/gmail/callback` | `web.routers.google.gmail_oauth_callback` | test_api.py |
| `POST` | `/pipeline/activity` | `web.api.pipeline_activity` | test_api.py, test_pipeline_ingest.py |
| `POST` | `/pipeline/aggregate` | `web.api.pipeline_aggregate` | test_api.py, test_crm_polling.py, test_pipeline_ingest.py |
| `POST` | `/pipeline/enrich` | `web.api.pipeline_enrich` | test_api.py, test_pipeline_ingest.py |
| `POST` | `/pipeline/generate` | `web.api.pipeline_generate` | test_api.py |
| `POST` | `/pipeline/ingest` | `web.api.pipeline_ingest` | test_api.py, test_authenticated_e2e.py, test_pipeline_ingest.py |
| `POST` | `/pipeline/partner-signals` | `web.api.pipeline_partner_signals` | test_api.py, test_pipeline_ingest.py |
| `POST` | `/pipeline/score` | `web.api.pipeline_score` | test_api.py, test_pipeline_ingest.py, test_require_auth_per_user_routing.py |
| `POST` | `/pipeline/sources` | `web.api.upload_pipeline_sources` | test_api.py, test_discovery_opt_in.py, test_investors_global.py (+2) |
| `POST` | `/pipeline/verify` | `web.api.pipeline_verify` | test_api.py, test_pipeline_ingest.py |

## review (2)

| Method | Path | Handler | Tests |
|---|---|---|---|
| `GET` | `/drafts/approved` | `web.api.get_approved` | test_api.py |
| `GET` | `/review/pending` | `web.api.get_pending` | test_api.py, test_authenticated_e2e.py, test_today_envelope.py (+2) |

## sequences (3)

| Method | Path | Handler | Tests |
|---|---|---|---|
| `GET` | `/sequences/{partner_id}` | `web.routers.sequences.get_sequence` | test_sequence_auto_stop.py, test_sequences.py |
| `POST` | `/sequences/{sequence_id}/skip` | `web.routers.sequences.skip_sequence` | test_sequence_auto_stop.py, test_sequences.py |
| `POST` | `/sequences/{sequence_id}/stop` | `web.routers.sequences.stop_sequence` | test_sequence_auto_stop.py, test_sequences.py |

## settings (3)

| Method | Path | Handler | Tests |
|---|---|---|---|
| `GET` | `/settings/email-samples` | `web.routers.email_samples.list_email_samples` | test_email_samples.py |
| `POST` | `/settings/email-samples` | `web.routers.email_samples.add_email_sample` | test_email_samples.py |
| `DELETE` | `/settings/email-samples/{sample_id}` | `web.routers.email_samples.delete_email_sample` | test_email_samples.py |

## status (2)

| Method | Path | Handler | Tests |
|---|---|---|---|
| `GET` | `/check_ready` | `web.api.check_ready` | test_api.py, test_check_ready.py, test_init_wizard.py (+1) |
| `GET` | `/runs` | `web.api.get_runs` | test_api.py, test_authenticated_e2e.py, test_per_user_endpoint_coverage.py (+1) |


# Feature Catalog (non-HTTP surfaces)

Subsystems, pipeline stages, background hooks, and internal modules. Each entry maps to test files that mention its symbol (function or module name).


## Approval (3)

| Feature | Symbol | Tests | Purpose |
|---|---|---|---|
| State machine — transitions table | `_TRANSITIONS` | **❌ no test mentions this symbol** | 11 declared edges; STATE_SENT is terminal |
| Approval gate — can_approve_draft | `can_approve_draft` | test_stale_on_manual_mutation.py, test_superseded_approval_refusal.py | Hard (DNC, bad email, smell) + soft blockers; override flag |
| Stale-after-approval invalidation | `stale_live_approvals_for_partner` | **❌ no test mentions this symbol** | Email change / DNC / relationship / score change triggers |

## Auth (3)

| Feature | Symbol | Tests | Purpose |
|---|---|---|---|
| Supabase JWT verification | `_verify_supabase_jwt` | test_api.py | HS256 with SUPABASE_JWT_SECRET |
| Legacy API_KEY fallback | `_is_api_key_fallback_enabled` | **❌ no test mentions this symbol** | AUTH_ALLOW_API_KEY_FALLBACK gate during cutover |
| Per-user workspace routing (contextvar) | `_CURRENT_USER_ID_VAR` | test_per_user_workspace.py, test_require_auth_per_user_routing.py | Async middleware stamps per-request; _engine_and_ws() reads |

## CRM (5)

| Feature | Symbol | Tests | Purpose |
|---|---|---|---|
| Attio activity poller (B6, real) | `list_activities_since` | test_crm_polling.py | Real HTTP to Attio /tasks; only Attio surface actually fetching |
| Attio pipeline-updates poller (stubbed) | `list_pipeline_updates_since` | test_crm_polling.py, test_sequence_auto_stop.py | Returns [] in production — needs per-tenant schema mapping |
| Attio investors poller (stubbed) | `list_investors` | test_crm_b7_b8_b9.py | Returns [] in production — same reason |
| Outbound Stage 8 sync | `find_partner_record` | test_stage8_approved_first.py, test_stage8_attio.py, test_stage8_find_partner_record.py (+1) | Email → linkedin_url → name+company match cascade against Attio |
| Encrypted credential storage (Fernet) | `decrypt_api_key` | test_crm_connections.py | CRM_ENCRYPTION_KEY-bound Fernet symmetric encryption at rest |

## Capture (3)

| Feature | Symbol | Tests | Purpose |
|---|---|---|---|
| QR capture endpoint | `capture_investor` | **❌ no test mentions this symbol** | POST /investors/capture with LinkedIn URL dedup + collision handling |
| LinkedIn URL normalization | `_normalize_linkedin_url` | **❌ no test mentions this symbol** | Strip scheme/www/trailing slash/query for canonical dedup |
| Partner-id collision suffix allocator | `_allocate_unique_partner_id` | **❌ no test mentions this symbol** | Two different people same firm+name slug → -2, -3, ... suffix |

## Channels (3)

| Feature | Symbol | Tests | Purpose |
|---|---|---|---|
| Per-partner channel preference | `channel_pref` | test_investor_endpoints.py, test_investors_capture.py | email / linkedin / both — column persisted |
| LinkedIn mark-sent (FR-7) | `mark_draft_sent` | **❌ no test mentions this symbol** | Operator-paste flow; logs outreach_events(source='app', channel='linkedin') |
| Mark-sent clear (revert) | `clear_draft_sent` | **❌ no test mentions this symbol** | Bypasses state machine (sent-is-terminal); writes audit row |

## Discovery (3)

| Feature | Symbol | Tests | Purpose |
|---|---|---|---|
| Discovery pool find_matches | `find_matches` | test_discovery.py | Ranked global pool reads, filtered by tenant's known partners |
| Discovery claim | `claim_investor` | test_discovery.py, test_review_small_fixes.py | Upsert fund + partner; stamp claimed_from_global_id |
| Per-tenant discovery opt-in (default OFF) | `_read_discovery_opt_in` | **❌ no test mentions this symbol** | Operator-level kill switch INVESTORS_GLOBAL_DISABLED also gates |

## Drafting (5)

| Feature | Symbol | Tests | Purpose |
|---|---|---|---|
| Strategy eligibility (6 strategies) | `compute_eligibility` | test_email_strategy_eligibility.py | Score each of the 6 strategies 0-3 for a partner |
| Batch QA — similarity | `ratio_similarity` | **❌ no test mentions this symbol** | Pairwise body/subject/first-sentence similarity hard gates |
| Batch QA — template smell | `template_smell_judge` | test_email_batch_qa.py | LLM (or heuristic in stub) judges template-smell vs neighbors |
| Banned-phrase + hard-gate enforcement | `check_hard_gates` | test_email_batch_qa.py, test_email_draft_routing.py, test_email_prompt.py (+1) | Forbidden phrases, raise reference, soft CTA, etc. |
| Operator voice samples (Stage 7 prompt injection) | `load_voice_samples_for_prompt` | test_email_samples.py | Up to 3 most-recent samples injected as OPERATOR_VOICE_SAMPLES |

## Gmail (5)

| Feature | Symbol | Tests | Purpose |
|---|---|---|---|
| Gmail OAuth bootstrap | `gmail_bootstrap` | **❌ no test mentions this symbol** | Per-user token persisted under workspace/secrets/ |
| Gmail sent polling | `poll_gmail_sent_for_workspace` | test_outreach_sent.py, test_today_snoozes_and_draft_link.py | Reads Sent box → outreach_events; HWM-resumable |
| Gmail reply polling | `poll_gmail_replies_for_workspace` | test_outreach_replies.py, test_reply_classifier_llm.py | Reads Inbox → outreach_events (event_type='replied') |
| Reply classifier (heuristic + LLM fallback) | `classify_reply` | test_operator_clis.py, test_outreach_replies.py, test_reply_classifier_llm.py | Cheap heuristic first; Claude only when 'unclear' |
| Gmail draft push (operator sends manually) | `create_gmail_drafts` | test_approval_gates.py, test_config_and_validators.py, test_launch_path_e2e.py (+1) | scripts/create_gmail_drafts.py creates draft objects in operator's account |

## Meeting prep (2)

| Feature | Symbol | Tests | Purpose |
|---|---|---|---|
| Dossier builder | `core.meeting_prep.dossier` | **❌ no test mentions this symbol** | LLM-driven dossier; eligibility-gated; cache by signal-set hash |
| Drive push (idempotent) | `drive_sync` | test_drive_sync.py | Upload to operator's Drive; skip if already pushed for the hash |

## Onboarding (4)

| Feature | Symbol | Tests | Purpose |
|---|---|---|---|
| Deck extract — PDF | `extract_text` | test_deck_extraction.py | pypdf parser with per-page tagging + corrupt-PDF tolerance |
| Deck extract — PPTX | `python-pptx` | test_deck_extraction.py | Per-slide tagging; legacy .ppt refusal warning |
| Production-mode stub refusal (P1 safety) | `_deck_stub_response` | **❌ no test mentions this symbol** | Refuses to return 'Stub Co' when ANTHROPIC_API_KEY missing in prod |
| Init wizard scaffolding | `init_wizard` | test_init_wizard.py | Interactive creation of company.yaml / sources / examples |

## Operations (4)

| Feature | Symbol | Tests | Purpose |
|---|---|---|---|
| _sync_columns_with_metadata (auto-migrate) | `_sync_columns_with_metadata` | test_sources_registry.py | ALTER TABLE ADD COLUMN with DEFAULT clause (audit-fix) |
| Migration registry | `MIGRATIONS` | test_migrations.py | m001..m004; applied_migration_ids tracks applied |
| Send-pace setting + hard daily cap enforcement | `_read_send_pace` | **❌ no test mentions this symbol** | 1-20 clamped; counted against outreach_events sent today |
| Snooze + future-ISO parser | `parse_future_iso_naive_utc` | **❌ no test mentions this symbol** | Shared parser; tz-naive UTC end-to-end |

## Pipeline stages (8)

| Feature | Symbol | Tests | Purpose |
|---|---|---|---|
| Stage 1 — aggregate_sources | `01_aggregate_sources` | test_authenticated_e2e.py, test_backups.py, test_config_and_validators.py (+11) | Load RSS / funding-announcement feeds into source_snapshots |
| Stage 2 — enrich_funds | `02_enrich_funds` | test_authenticated_e2e.py, test_backups.py, test_config_and_validators.py (+10) | Scrape fund homepage / portfolio / team; LLM-extract thesis |
| Stage 3 — mine_activity | `03_mine_activity` | test_authenticated_e2e.py, test_backups.py, test_config_and_validators.py (+8) | LLM-attribute funding announcements to funds + partners |
| Stage 4 — mine_partner_signals | `04_mine_partner_signals` | test_authenticated_e2e.py, test_backups.py, test_config_and_validators.py (+8) | Scrape LinkedIn/podcasts/blogs; LLM extract signals |
| Stage 5 — verify_and_quality | `05_verify_and_quality` | test_authenticated_e2e.py, test_backups.py, test_jobs.py (+5) | Re-fetch signal URLs + deterministic 0-3 quality scoring |
| Stage 6 — score_candidates | `06_score_candidates` | test_api.py, test_backups.py, test_jobs.py (+4) | Composite fit + round fit + lead likelihood + send-now priority |
| Stage 7 — generate_emails | `07_generate_emails` | test_api.py, test_apollo_workflow.py, test_approval_clis.py (+23) | LLM email drafts (6 strategies × 2 variants) + batch QA |
| Stage 8 — sync_to_attio | `08_sync_to_attio` | test_backups.py, test_config_and_validators.py, test_stage8_approved_first.py (+4) | Push partners + scores + outreach status to Attio CRM |

## Sequences (5)

| Feature | Symbol | Tests | Purpose |
|---|---|---|---|
| auto_stop_sequence_if_active | `auto_stop_sequence_if_active` | test_sequence_auto_stop.py | Idempotent cadence-gated sequence stop |
| Reply auto-stop wiring (B3 reconcile) | `reconcile_drafts_for_workspace` | test_outreach_replies.py, test_sequence_auto_stop.py | Joins outreach_events to sequences; only post-create replies |
| CRM-pipeline auto-stop wiring (B6 poll) | `poll_crm_pipeline_for_workspace` | test_crm_polling.py, test_sequence_auto_stop.py | Compares prior vs new stage; fires only on change |
| Cadence presets (standard / patient / aggressive) | `_PRESETS` | **❌ no test mentions this symbol** | Three preset shapes accessible via /settings/cadence/preset |
| Follow-up draft builder (FR-5) | `build_follow_ups_for_workspace` | test_followup_builder.py | Daily build; prior-send gate; implied due-at; per-angle prompts |

---

## Coverage summary

- **HTTP endpoints**: 78/80 have at least one test mention
- **Non-HTTP surfaces**: 37/53 have at least one test mention

_'Test mention' = grep against `tests/` finds the path or symbol. It's a low-bar coverage signal: a test mentioning a feature doesn't guarantee correctness. Use this as a starting point for verification, not the final answer._