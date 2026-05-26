# Build Session 12 (proposed): Meeting Prep — Objection Map + Framing Brief

Status: **drafted, not yet started.** Reviewed against the Founder's Corner
"two-minute investor briefing" article on 2026-05-26; covers prompts 3 + 5 of
that pipeline (the parts not already in this codebase). Prompts 1, 2, 6 are
out of scope because Stages 2 + 4 + `scripts/prep_brief.py` already cover
them with better data (verified signals + URLs).

## Goal

After Stage 7 books a meeting (or earns a substantive reply), produce two
extra artifacts per partner that make the *actual conversation* better.
Extends `scripts/prep_brief.py` — does **not** add a new pipeline stage.

## Non-goals

- ❌ Affecting cold-outreach emails. The objection map / framing brief is too
  rich for a 3-sentence cold drop; it's pre-meeting prep, not pre-send.
- ❌ Portfolio overlap analysis (article's Prompt 4). Requires Stage 2 to
  start persisting per-fund `portfolio_companies`, which is its own half-day.
  Punt to a separate session.
- ❌ Live web research at brief time. Stage 4 already mines partner signals;
  this session synthesizes from what's verified, not re-scrapes.
- ❌ Running on every partner. Gate to
  `outreach_status IN ('replied', 'meeting_booked')` so LLM spend tracks
  human signal.

## What gets built

Two pure LLM builders + schemas + prompts, plus a `prep_brief.py` flag
wiring.

```
core/meeting_prep/
  __init__.py
  objection_map.py        # builder; reads verified signals + fund kill signals
  framing_brief.py        # builder; reads everything + company.yaml + objection map
schemas/
  objection_map.py        # ObjectionMapV1
  framing_brief.py        # FramingBriefV1
prompts/
  objection_map.txt
  framing_brief.txt
scripts/
  prep_brief.py           # extended: --include-objections --include-framing flags
core/db.py                # new table: meeting_prep_artifacts (caches LLM output)
```

## Pydantic schemas (sketch)

```python
class Objection(BaseModel):
    objection: str
    underlying_concern: str
    source: Literal[
        "stated_thesis", "portfolio_pattern", "public_position", "sector_norm",
    ]
    citing_signal_ids: list[int]  # MUST be non-empty if source != sector_norm
    strong_answer_hint: str
    weak_answer_hint: str
    severity: Literal["high", "medium", "low"]


class ObjectionMapV1(BaseModel):
    partner_id: str
    objections: list[Objection]   # 5-7 items
    insufficient_evidence: bool   # True if <2 quality-≥2 signals -> skip narrative
    notes: str = ""


class FramingBriefV1(BaseModel):
    partner_id: str
    lead_with: str
    amplify: list[str]              # 2-3 angles backed by signal_ids
    address_unprompted: list[str]   # objections to preempt
    do_not_lead_with: list[str]     # patterns this partner has criticized publicly
    question_to_ask_them: str
    citing_signal_ids: list[int]
```

## Hard rules (match the brief's existing conventions)

1. **Every objection must cite at least one verified, quality-≥2 signal_id**
   (unless `source = "sector_norm"`, in which case it's a generic VC
   objection clearly labeled). No invented psychology.
2. **If a partner has fewer than 2 quality-≥2 signals**, the builder returns
   `insufficient_evidence=True` and writes a one-line note instead of
   fabricating. Same discipline as Gate 5's signal floor.
3. **Cache in `meeting_prep_artifacts` keyed on
   `(partner_id, signal_set_hash)`.** Re-running `prep_brief` is free unless
   the partner's signal set changed. Mirrors the existing `source_snapshots`
   `content_hash` pattern.
4. **No new ceiling on send / ready_to_send.** This is post-send
   infrastructure; Rule 16 doesn't apply.

## Inputs (all from existing tables)

- `signals` joined to `source_snapshots` (verified=True, quality≥2)
- `partners` + `partner_score_summaries` (composite, axis breakdowns, kill signals)
- `deal_attributions` for portfolio-pattern objections
- `funds.kill_signals`
- `company.yaml` — the new `company:` block (`problem`, `solution`,
  `differentiators`, `desired_traits`, `excluded_sectors` all become relevant
  here, which is finally where those fields earn their keep)

## CLI surface

```bash
uv run scripts/prep_brief.py \
  --partner-id p_acme_partner_jane \
  --include-objections --include-framing \
  --out clients/{ws}/exports/briefs/jane.md
```

Defaults: if `outreach_status IN ('replied','meeting_booked')`, both flags
imply `true` unless explicitly disabled. Other statuses require explicit
opt-in (`--include-objections`) so cold-pipeline partners don't burn LLM
time.

## Output shape

Single markdown file. Existing `prep_brief` sections (fund snapshot, scores,
top quotes, recommended email) stay at the top; two new sections appended:

```
## Objections to prepare for
1. **API concentration risk** [from podcast, 2024-09-12]
   - Underlying concern: ...
   - Strong answer: ...
   - Weak answer (avoid): ...

## How to tell your story today
- **Lead with:** ...
- **Amplify:** ...
- **Address unprompted:** ...
- **Do not lead with:** ...
- **Question to ask them:** ...
```

## Test plan (fixture-first, matches existing convention)

- 3 fixture partners in
  `clients/test_workspace/data/fixtures/meeting_prep_seed.json`:
  - one with rich signals (full output)
  - one with thin signals (`insufficient_evidence=True` path)
  - one with conflicting signals across axes (variance handling)
- Pydantic schema validation on every LLM call (retry 3x on bad JSON, same
  as Stage 4/6/7)
- Cache hit test: re-running with unchanged signals must produce zero LLM
  calls
- One snapshot test: generated markdown round-trips through
  `prep_brief --partner-id X --include-objections --include-framing`

## Budget

- 1 LLM call per partner per builder = 2 calls per `prep_brief` invocation
- Sonnet, not Opus (this is structured analysis, not voice generation)
- Expected: ~6¢ per partner, runs on-demand not on every Stage 7 batch
- 4–6 hours build time (~3 hours coding, 1–2 hours fixture + tests)

## What this unlocks for prompts you've been wanting to use

The new `company.yaml` fields added in the onboarding wizard (`problem`,
`solution`, `differentiators`, `why_now`, `desired_traits`,
`excluded_sectors`, `do_not_contact`) currently sit in the dict but aren't
consumed by Stage 6/7 prompts. The framing-brief prompt is the natural first
consumer:

- `differentiators` → drives "what to amplify"
- `excluded_sectors` → flags portfolio adjacencies to deprioritize
- `desired_traits` → "what to ask them"
- `problem` / `solution` → calibrates the "lead with" recommendation against
  partner stated positions

That's the right place to start using them — meeting prep is a
higher-signal context than a cold drop.
