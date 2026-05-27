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
