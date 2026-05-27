# Investor Outreach

**Cold investor outreach that sounds warm. Every email reviewed by you before it leaves.**

Raising is a research problem before it's a writing problem. Most founders lose two weeks of the round to building a target list, hunting partner emails, and rewriting the same intro 80 times — then either send something generic, or don't send at all.

This collapses that work into a few hours, without trading away the part that matters: a thoughtful, specific note from you to each partner, that you actually read before it goes out.

## What you get

- **A scored target list of partners, not firms.** Sector, stage, and geography fit weighed against each partner's recent activity, deal history, and stated thesis.
- **Drafts in your own voice.** Upload a few examples; every draft hooks into a real signal — a recent investment, a post, a podcast quote — not boilerplate.
- **No auto-send. Ever.** Approved drafts land in your Gmail Drafts folder. You hit Send. Or you export a CSV and send from wherever you already work.
- **A Today queue that stays honest.** A ranked daily batch sized to your send-pace, with a "next up" preview and a remaining-count badge so you always know where you stand. Snooze any draft to push it forward.
- **Follow-ups that know when to stop.** Per-partner sequences with configurable cadence (standard / patient / aggressive) that auto-halt the moment someone replies, advances in your CRM, or you manually pass.
- **A QR-based capture at events.** Scan a partner's LinkedIn from your phone; they're deduped and in your pipeline before you leave the booth.
- **Meeting prep when you need it.** When a partner replies or books, you get a dossier — how they think, firm snapshot, fit framing, likely objections — pushed to your Drive as a Google Doc.
- **CRM sync if you want it.** Optional Attio integration keeps approved outreach, replies, and outcomes in your existing pipeline. Skip it and the rest works the same.

## How it works

1. **Sign up with Google.** One consent step covers Gmail drafts and Drive dossiers.
2. **Upload your pitch deck.** A draft company profile gets extracted — one-liner, stage, traction, ICP, target investor criteria. You review and confirm.
3. **Bring your investor list, or start from scratch.** Drop in an OpenVC export, an Apollo CSV, or a list you've been keeping in a spreadsheet. Partner names, recent deals, fund focus, and contact info get enriched automatically.
4. **Review the recommendations.** Partners are ranked by fit with the evidence behind each score — so you know *why* before you decide to reach out.
5. **Approve drafts one at a time.** Each email cites a concrete reason this partner is the right person, in your voice. Approve, reject, or edit.
6. **Send on your terms.** Approved drafts go to your Gmail Drafts. You send them when you're ready.
7. **Reply lands? Get a brief.** When a partner replies substantively or books a meeting, a dossier gets prepared so you walk in informed.

## Why this, instead of a sequencer

The mass-sequencer playbook works for SaaS. It does not work for venture. Partners read every cold email, recognize templates, and forward bad ones to the group chat. The first impression is the round.

This is built around the opposite stance:

- **Approval-gated by design.** Nothing leaves without you reading it. The product refuses to send. We mean this — there is no auto-send flag.
- **Deliverability discipline built in.** Daily caps, do-not-contact lists, duplicate-recipient checks, and stale-approval invalidation when the underlying evidence changes.
- **Honest about scope.** Cold email gets you the meeting. The dossier gets you ready for it. A cold email shouldn't run 600 words.
- **Quality bar enforced.** Drafts that fail internal QA (off-voice, unsupported claim, missing hook) never reach your inbox.

## Under the hood

The full capability surface, grouped. Skip to the parts that matter to you.

**Discovery & enrichment**
- Multi-source ingest: OpenVC, Apollo, CSV upload, RSS, funding-announcement feeds
- LLM fund enrichment (thesis, sectors, stage focus, check-size, current partners) via Firecrawl-scraped homepage / portfolio / team / thesis pages
- Per-partner signal harvesting from LinkedIn, podcasts, blogs
- Live re-verification of every cited signal before it counts
- Deal attribution links funding announcements back to funds + partners
- Provisional-fund handling for incomplete domains (DNC'd until claimed)
- QR-based capture with LinkedIn-URL dedup
- Per-partner channel preference (email / LinkedIn / both)

**Scoring**
- Composite thesis fit (LLM)
- Round fit (deterministic stage + check-size match)
- Lead likelihood (seniority + tenure)
- Combined "send-now" priority
- "Why now" rationale rendered on every recommendation card

**Drafting & QA**
- LLM email generation per partner with two variants
- Six strategy frameworks scored for eligibility per-partner
- Batch QA: pairwise similarity, template-smell distribution, hard / soft gates
- Operator-voice mirroring from uploaded samples
- Stale-after-approval state machine when underlying evidence shifts

**The Today queue**
- Stable per-day ranked batch
- Envelope payload: `drafts` + `next_drafts` preview + `total_remaining` badge
- Per-pick gate hydrated server-side (no second round-trip from the UI)
- Send-pace control (1-20 / day, per-workspace)
- Approve / reject with audit-required notes; override flag for soft blockers
- Snooze with ISO-future expiry; `until: null` clears

**Sequences & follow-ups**
- Per-partner state machine: active / stopped / completed
- Configurable cadence: max-touches, per-touch gap_days + angle, daily mix
- Three presets: standard (4 touches), patient (5), aggressive (4 tight)
- Four toggleable auto-stop signals: reply, CRM pipeline-advance, manual pass, fund news
- Operator skip (defer N days without consuming a touch) + stop (whitelist reason, idempotent)
- Touch angles: `new_signal` / `specific_ask` / `soft_check_in` / `graceful_close` / `custom`

**Sending & reconciliation (Gmail)**
- Approved drafts pushed to Gmail Drafts; operator hits Send
- Sent box polled every 10 min → `outreach_events`
- Inbox polled every 10 min for replies + LLM classifier (positive / negative / neutral)
- Unread reply count surfaced via `/replies`
- First reply auto-stops the sequence (configurable)

**Meeting prep**
- Auto-dossier on substantive reply or meeting booked
- Firm snapshot, fit framing, likely objections
- Pushed to operator's Google Drive as a Google Doc

**CRM (optional Attio)**
- Outbound push of partners, scores, outreach status (Stage 8)
- Six background pollers: activity (15 min), pipeline (30 min, auto-stops sequences), investors (6 h), relationships (6 h), lists (1 h), deals (1 h)
- One-shot bulk import on connect
- Manual-override + preserve-on-outreach respected so the CRM stays in charge of fields it owns
- Encrypted credential storage (Fernet)

**Onboarding**
- 5-stage guided wizard
- Deck extraction (PDF / PPTX) → drafted company profile
- Source-file upload + header validation
- One-button pipeline kick-off

**Privacy & multi-tenancy**
- Per-user SQLite workspace (the workspace IS the tenant boundary — nothing leaks across)
- Per-tenant discovery-pool opt-in, default OFF
- Operator kill switch (`INVESTORS_GLOBAL_DISABLED=true`)
- Per-partner do-not-contact enforced at the approval gate, with audit metadata

**Platform**
- Supabase JWT (HS256) auth + legacy API_KEY fallback for migration
- 50+ REST endpoints, OpenAPI auto-generated at `/openapi.json`
- 9 cron-style hook endpoints (`X-Hook-Secret` auth, fail-closed)
- Fly.io deployable (Dockerfile + fly.toml, persistent SQLite volume at `/data`)
- 8-shard parallel CI (~90s wall time on PR feedback)

## What it does *not* do

- It does not send email automatically. Drafts only.
- It does not run a warm-intro workflow. Use your network for that.
- It does not guarantee a partner's email — that comes from your enrichment source (Apollo, OpenVC, manual).
- It does not require Attio, Apollo, or any specific CRM. Use what you already use.
- It does not run a dossier for every cold prospect. Dossier depth is reserved for partners who replied or booked.

## Get access

Currently in private beta with founders actively running pre-seed and seed rounds.

→ **Reach out:** [ashleytrick@gmail.com](mailto:ashleytrick@gmail.com)
