"""Scaffold a new workspace directory with template config + example stubs.

Run: uv run scripts/init_workspace.py oko_seed
  -> creates clients/oko_seed/ with all required subdirs and template files.

The templates are intentionally minimal placeholders. The operator must edit
them with real company / axes / source info before running the pipeline.
The script refuses to overwrite an existing workspace unless --force is set.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

COMPANY_TEMPLATE = """\
# Required. Replace every {PLACEHOLDER} with real values before any real run.
company:
  name: "{COMPANY_NAME}"
  founder_name: "{FOUNDER_NAME}"
  founder_email: "{FOUNDER_EMAIL}"
  one_liner: "{ONE_SENTENCE_DESCRIPTION}"
  description: |
    {LONGER_PARAGRAPH_DESCRIPTION_INCLUDING_TRACTION_AND_CATEGORY}
  current_traction:
    headline_metric: "{e.g., $X ARR, X paying customers}"
    secondary_metrics:
      - "{e.g., NRR 130%}"
      - "{e.g., 4 design partners signed}"
  stage: "{PRE_SEED|SEED|SERIES_A}"
  target_check_size_usd:
    min: 250000
    max: 1500000
  target_geographies:
    - "United States"
  # Used by Stage 6 round_fit recent_relevant_deals scoring. Sector terms
  # that, when present in a fund's stated_thesis or a deal's sector_tags,
  # signal adjacency to your company.
  target_sectors:
    - "{sector_keyword_1}"
    - "{sector_keyword_2}"
  adjacent_companies:
    - "{ADJACENT_CO_1}"
  anchor_funds:
    - "{ANCHOR_FUND_1}"
  meeting_ask:
    duration_minutes: 30
    format: "video call"
    preferred_scheduling_link: "{CALENDLY_OR_OTHER_URL}"

# Mandatory. The system only operates when an active raise is in progress.
raise_context:
  round: "{Seed | Series A | etc.}"
  amount: "{TARGET_RAISE}"
  instrument: "{priced | SAFE | convertible note | TBD}"
  status: "{opening conversations | actively in market | first checks soft-circled}"
  timing: "{target first close date / final close date / decision window}"
  use_of_funds:
    - "{milestone 1}"
    - "{milestone 2}"
  why_this_round_is_fundable_now: "{One sentence}"
  what_changes_after_this_round: "{One sentence}"
  strongest_raise_proof: "{Founder-designated best proof}"
  round_hook:
    strongest_reason_to_meet_now: "{One sentence}"
    investor_consequence_of_waiting: "{What they miss if they wait}"
    round_momentum_proof: "{soft-circled checks, customer milestone, etc.}"
  notable_existing_investors_or_non_dilutive: "{Optional}"

round_fit:
  must_have:
    - "invests at this stage"
    - "can write or lead target check size"
    - "has made at least one new investment in last 12 to 18 months"
  nice_to_have:
    - "has led comparable rounds"
    - "has reserves for follow-on"
    - "partner-level conviction in category"
  disqualifiers:
    - "growth-only investor"
    - "pre-seed-only when raising seed (or seed-only when raising A)"
    - "follow-on capital only, never leads"
    - "not currently deploying capital"
    - "check size constraint mismatched to this round"

founder_voice:
  style: "{e.g., direct, serious, high-conviction, not hypey. Few words, no buzzwords.}"
  banned_phrases:
    - "would love"
    - "excited to"
    - "game-changing"
    - "synergy"
  preferred_phrases:
    - "{phrases the founder naturally uses}"
  example_emails_path: "prompts/examples/"
"""

AXES_TEMPLATE = """\
# 4 belief/psychology axes that DISCRIMINATE between investors. Test for
# orthogonality: if any plausible investor would always score together on
# two axes, collapse them.
axes:
  - id: axis_1
    name: "{AXIS_1_NAME}"
    description: "{ONE_SENTENCE_BELIEF}"
    positive_signals:
      - "{KEYWORD_1}"
      - "{KEYWORD_2}"
    negative_signals:
      - "{ANTI_SIGNAL}"
    weight: 1.0
  - id: axis_2
    name: "{AXIS_2_NAME}"
    description: "{ONE_SENTENCE_BELIEF}"
    positive_signals:
      - "{KEYWORD_1}"
    negative_signals:
      - "{ANTI_SIGNAL}"
    weight: 1.0
  - id: axis_3
    name: "{AXIS_3_NAME}"
    description: "{ONE_SENTENCE_BELIEF}"
    positive_signals:
      - "{KEYWORD_1}"
    negative_signals:
      - "{ANTI_SIGNAL}"
    weight: 1.0
  - id: axis_4
    name: "{AXIS_4_NAME}"
    description: "{ONE_SENTENCE_BELIEF}"
    positive_signals:
      - "{KEYWORD_1}"
    negative_signals:
      - "{ANTI_SIGNAL}"
    weight: 1.0
"""

SOURCES_TEMPLATE = """\
public_lists:
  - name: "OpenVC Export"
    path: "data/raw/openvc_export.csv"
    parser: "csv"
# Add more here. Example:
#   - name: "GitHub Awesome List"
#     url: "https://raw.githubusercontent.com/..."
#     parser: "markdown"
funding_announcement_feeds:
  - name: "TechCrunch Funding"
    url: "https://techcrunch.com/category/venture/feed/"
    parser: "rss"
partner_signal_sources:
  podcast_search_api: "listennotes"
  substack_search: true
"""

ATTIO_TEMPLATE = """\
# OPTIONAL. Uncomment if you sync to Attio.
# attio:
#   workspace_id: "{ATTIO_WORKSPACE_ID}"
#   api_base: "https://api.attio.com/v2"
#   matching_attributes:
#     companies: "domains"
#     people: "email_addresses"
#   objects:
#     funds: "companies"
#     partners: "people"
#   # See PROJECT_BRIEF.md for the full fund_attributes / partner_attributes
#   # mappings and preserve_on_outreach_started / manual_override_protection.
"""

ENV_TEMPLATE = """\
# Workspace-specific keys; override repo-root .env when both define a key.
# ANTHROPIC_API_KEY=sk-ant-...
# ATTIO_API_KEY=...
"""

EXAMPLE_STUB = """\
# {STRATEGY} examples for {WORKSPACE}.
# Replace these placeholders with 3 real signal-led examples (or 2 for follow-up
# and deck_request_response). The LLM uses these as STYLE ANCHORS.
# Per the Client Onboarding Requirement, you need a minimum of:
#   3 signal_led, 3 portfolio_led OR market_shift_led, 2 follow_up,
#   2 deck_request_response.

## Example 1

Subject: ...

Body (replace with your actual prose):
...
"""

EXAMPLE_FILES = [
    "signal_led.md",
    "portfolio_led.md",
    "round_pattern_led.md",
    "market_shift_led.md",
    "contrarian_thesis_led.md",
    "traction_led.md",
    "follow_up.md",
    "deck_request_response.md",
]


def main() -> int:
    parser = argparse.ArgumentParser(description="Scaffold a new workspace.")
    parser.add_argument("name", help="Workspace short name, e.g. oko_seed.")
    parser.add_argument(
        "--force", action="store_true",
        help="Overwrite an existing workspace directory.",
    )
    args = parser.parse_args()

    name = args.name.strip().replace(" ", "_")
    ws_path = REPO_ROOT / "clients" / name
    if ws_path.exists() and not args.force:
        print(
            f"workspace already exists: {ws_path}\n"
            f"pass --force to overwrite, or pick another name."
        )
        return 2

    # Directory tree.
    for sub in (
        "config",
        "data/raw",
        "data/fixtures",
        "exports",
        "prompts/examples",
    ):
        (ws_path / sub).mkdir(parents=True, exist_ok=True)

    # Config templates.
    (ws_path / "config" / "company.yaml").write_text(COMPANY_TEMPLATE,
                                                    encoding="utf-8")
    (ws_path / "config" / "axes.yaml").write_text(AXES_TEMPLATE, encoding="utf-8")
    (ws_path / "config" / "sources.yaml").write_text(SOURCES_TEMPLATE,
                                                     encoding="utf-8")
    (ws_path / "config" / "attio.yaml").write_text(ATTIO_TEMPLATE, encoding="utf-8")
    (ws_path / ".env").write_text(ENV_TEMPLATE, encoding="utf-8")

    # Email-style example stubs.
    for fname in EXAMPLE_FILES:
        strategy = fname.removesuffix(".md")
        (ws_path / "prompts" / "examples" / fname).write_text(
            EXAMPLE_STUB.replace("{STRATEGY}", strategy).replace("{WORKSPACE}", name),
            encoding="utf-8",
        )

    # Resolved key status (root .env + this workspace's .env + process env).
    root_env = REPO_ROOT / ".env"
    has_root_anthropic = root_env.exists() and "ANTHROPIC_API_KEY=" in (
        root_env.read_text(encoding="utf-8") if root_env.exists() else ""
    ) and "ANTHROPIC_API_KEY=" + "\n" not in (
        root_env.read_text(encoding="utf-8") if root_env.exists() else ""
    )
    has_process_anthropic = bool(os.environ.get("ANTHROPIC_API_KEY"))
    anthropic_status = (
        "process env" if has_process_anthropic
        else "root .env" if has_root_anthropic
        else "MISSING (LLM will run in stub mode)"
    )

    print(f"created workspace: {ws_path}")
    print("  config/company.yaml   <- EDIT: name, raise_context, founder voice, target_sectors")
    print("  config/axes.yaml      <- EDIT: 4 orthogonal belief axes")
    print("  config/sources.yaml   <- EDIT: source list paths/URLs")
    print("  config/attio.yaml     <- EDIT: only if syncing to Attio")
    print("  prompts/examples/     <- EDIT: 3+ signal_led, 2+ follow_up, etc.")
    print("  .env                  <- EDIT: workspace-specific keys (overrides root .env)")
    print()
    print(f"anthropic key: {anthropic_status}")
    print()
    print("next: edit the above, then run:")
    print(f"  export INVESTOR_WORKSPACE=clients/{name}")
    print("  uv run scripts/01_aggregate_sources.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
