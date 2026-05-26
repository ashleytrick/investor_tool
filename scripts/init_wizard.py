"""Interactive setup wizard for a new workspace (Slice 14).

`scripts/init_workspace.py` scaffolds the directory tree + dumps
template config files full of `{PLACEHOLDER}` strings the operator
must edit by hand. This wizard goes further: it asks for the
mandatory fields up front, validates each, substitutes them into the
generated `company.yaml`, and -- when the operator picks
`mode: production` -- runs `validate_workspace_config` on the
freshly-scaffolded workspace so missing-API-key surprises don't
land at first-pipeline-run time.

Usage:

  # Interactive (default):
  uv run scripts/init_wizard.py <workspace_slug>

  # Non-interactive (CI, scripted re-init):
  uv run scripts/init_wizard.py oko_seed \\
      --company-name "Oko" --founder-name "Ashley Trick" \\
      --founder-email "ashley@oko.com" --mode dry_run \\
      --scheduling-link "https://cal.com/ashley/oko-vc" \\
      --one-liner "We index VC fund activity for founders" \\
      --target-sector "fintech" --target-sector "infrastructure" \\
      --non-interactive

  # Pre-existing workspace: refuse without --force; --force re-writes
  # only the template configs (preserves data/, exports/, edited
  # prompts/examples/).
"""
from __future__ import annotations

import argparse
import os
import pathlib
import re
import shutil
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

# Reuse the template constants + scaffolding from init_workspace so
# the two stay in lockstep -- wizard substitutes specific fields,
# everything else (axes, sources, attio, env, gitignore, example
# stubs) lands as the canonical template.
from scripts.init_workspace import (  # noqa: E402
    AXES_TEMPLATE,
    ATTIO_TEMPLATE,
    COMPANY_TEMPLATE,
    ENV_TEMPLATE,
    EXAMPLE_FILES,
    EXAMPLE_STUB,
    REPO_ROOT,
    SLUG_RE,
    SOURCES_TEMPLATE,
)


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_URL_RE = re.compile(r"^https?://", re.IGNORECASE)


class WizardAbort(Exception):
    """Raised when the operator pressed ^C / EOF mid-wizard or a
    non-interactive run is missing a required field."""


def _ask(prompt: str, *, default: str | None = None, validate=None) -> str:
    """Prompt the operator until validate() passes (or KeyboardInterrupt).

    `validate` returns None on success OR a string error to surface to
    the operator. `default` is shown in brackets and used when the
    operator just presses Enter.
    """
    suffix = f" [{default}]" if default is not None else ""
    while True:
        try:
            raw = input(f"{prompt}{suffix}: ").strip()
        except (EOFError, KeyboardInterrupt) as exc:
            raise WizardAbort("aborted by operator") from exc
        value = raw or (default or "")
        if validate is None:
            return value
        err = validate(value)
        if err is None:
            return value
        print(f"  -> {err}")


def _validate_slug(value: str) -> str | None:
    if not value:
        return "slug is required"
    if not SLUG_RE.match(value):
        return (
            "slug must contain only letters, digits, dashes, underscores "
            "(no slashes, no dots, no spaces)"
        )
    return None


def _validate_email(value: str) -> str | None:
    if not value:
        return "founder email is required"
    if not _EMAIL_RE.match(value):
        return f"{value!r} does not look like an email address"
    return None


def _validate_url(value: str) -> str | None:
    if not value:
        return "scheduling link is required"
    if not _URL_RE.match(value):
        return "scheduling link must start with http:// or https://"
    return None


def _validate_mode(value: str) -> str | None:
    if value not in ("fixture", "dry_run", "production"):
        return "mode must be one of: fixture | dry_run | production"
    return None


def _validate_nonempty(label: str):
    def _f(value: str) -> str | None:
        if not value.strip():
            return f"{label} is required"
        return None
    return _f


def _collect_answers_interactive(default_slug: str | None) -> dict:
    print()
    print("=" * 60)
    print("Setup wizard -- I'll ask for the mandatory fields, then")
    print("scaffold the workspace with your answers baked in.")
    print("=" * 60)
    print()
    slug = _ask(
        "workspace slug (also the clients/ directory name)",
        default=default_slug,
        validate=_validate_slug,
    )
    print()
    company_name = _ask("company name", validate=_validate_nonempty("company name"))
    founder_name = _ask("founder name", validate=_validate_nonempty("founder name"))
    founder_email = _ask("founder email", validate=_validate_email)
    one_liner = _ask(
        "one-sentence company description",
        validate=_validate_nonempty("one-liner"),
    )
    scheduling_link = _ask(
        "preferred scheduling link (Calendly / Cal.com / etc.)",
        validate=_validate_url,
    )
    print()
    print("Pick a workspace mode:")
    print("  fixture    -- ships test data only; no external writes")
    print("  dry_run    -- real data, integrations skipped on send "
          "(default for new workspaces)")
    print("  production -- real outreach; required integrations enforced")
    mode = _ask("mode", default="dry_run", validate=_validate_mode)
    print()
    print("Target sectors (comma-separated, e.g. 'fintech, infra'):")
    sectors_raw = _ask(
        "target sectors",
        validate=_validate_nonempty("target sectors"),
    )
    sectors = [s.strip() for s in sectors_raw.split(",") if s.strip()]
    return {
        "slug": slug,
        "company_name": company_name,
        "founder_name": founder_name,
        "founder_email": founder_email,
        "one_liner": one_liner,
        "scheduling_link": scheduling_link,
        "mode": mode,
        "target_sectors": sectors,
    }


def _collect_answers_non_interactive(args) -> dict:
    """Collect from CLI args; fail with a list of all missing fields."""
    missing: list[str] = []

    def _required(flag: str, value):
        if value is None or (isinstance(value, str) and not value.strip()):
            missing.append(flag)
        return value

    answers = {
        "slug": _required("workspace_slug", args.name),
        "company_name": _required("--company-name", args.company_name),
        "founder_name": _required("--founder-name", args.founder_name),
        "founder_email": _required("--founder-email", args.founder_email),
        "one_liner": _required("--one-liner", args.one_liner),
        "scheduling_link": _required("--scheduling-link", args.scheduling_link),
        "mode": args.mode or "dry_run",
        "target_sectors": list(args.target_sector or []),
    }
    if not answers["target_sectors"]:
        missing.append("--target-sector (one or more)")
    if missing:
        raise WizardAbort(
            f"--non-interactive requires: {', '.join(missing)}"
        )
    # Re-validate the non-interactive inputs.
    for field, validator in (
        ("slug", _validate_slug),
        ("founder_email", _validate_email),
        ("scheduling_link", _validate_url),
        ("mode", _validate_mode),
    ):
        err = validator(answers[field])
        if err:
            raise WizardAbort(f"{field}: {err}")
    return answers


def _company_yaml_from_answers(answers: dict) -> str:
    """Substitute the wizard's answers into the canonical
    COMPANY_TEMPLATE. Fields the wizard doesn't ask about stay as
    template `{PLACEHOLDER}` strings (the operator edits them later
    when the data exists -- raise_context, traction metrics, etc.).
    """
    text = COMPANY_TEMPLATE
    subs = {
        "{COMPANY_NAME}": answers["company_name"],
        "{FOUNDER_NAME}": answers["founder_name"],
        "{FOUNDER_EMAIL}": answers["founder_email"],
        "{ONE_SENTENCE_DESCRIPTION}": answers["one_liner"],
        "{CALENDLY_OR_OTHER_URL}": answers["scheduling_link"],
    }
    for placeholder, value in subs.items():
        text = text.replace(placeholder, value)
    # Replace the {sector_keyword_N} placeholders with the operator's
    # comma-separated list. Keep the YAML list formatting consistent.
    sector_block_lines = [
        f'    - "{s}"' for s in answers["target_sectors"]
    ]
    template_sector_block = (
        '    - "{sector_keyword_1}"\n'
        '    - "{sector_keyword_2}"'
    )
    text = text.replace(template_sector_block, "\n".join(sector_block_lines))
    # Inject the mode at the top of the YAML so it's the first thing
    # the operator sees -- and so `config_loader.py` picks it up
    # immediately on first read. COMPANY_TEMPLATE doesn't have a
    # `mode:` line by default (init_workspace's templating leaves the
    # operator to add one); the wizard's whole point is to NOT leave
    # that to chance.
    if "\nmode:" not in text:
        text = f"mode: {answers['mode']}\n" + text
    return text


def _write_workspace(ws_path: pathlib.Path, answers: dict, *,
                     force: bool) -> None:
    """Lay down the directory tree + templated configs. Mirrors
    init_workspace.main() but feeds COMPANY_TEMPLATE through the
    wizard's substitution layer first."""
    existed = ws_path.exists()
    if existed and not force:
        raise WizardAbort(
            f"workspace already exists at {ws_path}; pass --force to "
            f"re-write the template configs (data + exports preserved)."
        )
    # Directory tree.
    for sub in (
        "config", "data/raw", "data/fixtures", "exports", "prompts/examples",
    ):
        (ws_path / sub).mkdir(parents=True, exist_ok=True)
    # Config templates.
    (ws_path / "config" / "company.yaml").write_text(
        _company_yaml_from_answers(answers), encoding="utf-8",
    )
    (ws_path / "config" / "axes.yaml").write_text(AXES_TEMPLATE, encoding="utf-8")
    (ws_path / "config" / "sources.yaml").write_text(SOURCES_TEMPLATE, encoding="utf-8")
    (ws_path / "config" / "attio.yaml").write_text(ATTIO_TEMPLATE, encoding="utf-8")
    (ws_path / ".env").write_text(ENV_TEMPLATE, encoding="utf-8")
    (ws_path / ".gitignore").write_text(
        "# Per-workspace gitignore. Pipeline state + secrets + raw scraped\n"
        "# content + generated exports must NEVER be committed.\n"
        ".env\n"
        ".gmail_credentials.json\n"
        ".gmail_token.json\n"
        "data/pipeline.db\n"
        "data/pipeline.db-*\n"
        "data/raw/\n"
        "exports/\n"
        "*.bak.*\n",
        encoding="utf-8",
    )
    (ws_path / "data" / "raw" / "partner_content_urls.csv").write_text(
        "partner_id,source_type,source_url\n", encoding="utf-8",
    )
    for fname in EXAMPLE_FILES:
        strategy = fname.removesuffix(".md")
        (ws_path / "prompts" / "examples" / fname).write_text(
            EXAMPLE_STUB.replace("{STRATEGY}", strategy)
                       .replace("{WORKSPACE}", answers["slug"]),
            encoding="utf-8",
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Interactive setup wizard for a new workspace.",
    )
    parser.add_argument(
        "name", nargs="?", default=None,
        help="Workspace short name (e.g. oko_seed). Asked interactively "
             "when omitted.",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Re-write the template config files in an existing workspace. "
             "Leaves data/raw/, exports/, prompts/examples/ edits intact.",
    )
    parser.add_argument(
        "--non-interactive", action="store_true",
        help="Skip prompts; every required value must come from a CLI flag.",
    )
    parser.add_argument("--company-name", default=None)
    parser.add_argument("--founder-name", default=None)
    parser.add_argument("--founder-email", default=None)
    parser.add_argument("--one-liner", default=None)
    parser.add_argument("--scheduling-link", default=None)
    parser.add_argument(
        "--mode", default=None, choices=("fixture", "dry_run", "production"),
    )
    parser.add_argument(
        "--target-sector", action="append", default=None,
        help="Target sector keyword; repeat the flag for multiple "
             "(--target-sector fintech --target-sector infra).",
    )
    args = parser.parse_args()

    try:
        if args.non_interactive:
            answers = _collect_answers_non_interactive(args)
        else:
            answers = _collect_answers_interactive(args.name)
    except WizardAbort as exc:
        print(f"[wizard] REFUSED: {exc}")
        return 2

    ws_path = REPO_ROOT / "clients" / answers["slug"]
    try:
        _write_workspace(ws_path, answers, force=args.force)
    except WizardAbort as exc:
        print(f"[wizard] REFUSED: {exc}")
        return 2

    print()
    print(f"created workspace: {ws_path}")
    print(f"  mode:             {answers['mode']}")
    print(f"  company:          {answers['company_name']}")
    print(f"  founder:          {answers['founder_name']} <{answers['founder_email']}>")
    print(f"  scheduling link:  {answers['scheduling_link']}")
    print(f"  target sectors:   {', '.join(answers['target_sectors'])}")
    print()
    print("==  what's already filled in (no further edits needed)  ==")
    print("  config/company.yaml: company.name, founder_name, founder_email,")
    print("    one_liner, meeting_ask.preferred_scheduling_link,")
    print("    target_sectors, mode")
    print()
    print("==  still TODO before first real run  ==")
    print("  - Edit config/company.yaml -- raise_context (round, amount,")
    print("    timing, hooks), current_traction (headline metric), founder_voice,")
    print("    target_check_size_usd / target_geographies / adjacent_companies.")
    print("  - Edit config/axes.yaml -- four belief axes specific to your")
    print("    company / category.")
    print("  - Edit config/sources.yaml -- fund seed lists, announcement RSS,")
    print("    partner content sources you actually want to scrape.")
    print("  - Edit prompts/examples/*.md -- replace stubs with real founder-")
    print("    voice email examples (>=1 per strategy: signal_led, portfolio_led,")
    print("    market_shift_led, round_pattern_led, traction_led, follow_up,")
    print("    deck_request_response).")
    print("  - Fill .env with ANTHROPIC_API_KEY (live LLM) and -- when")
    print(f"    moving to mode: production -- ATTIO_API_KEY, etc.")
    print()
    print(f"next: export INVESTOR_WORKSPACE=clients/{answers['slug']}")
    print("      uv run scripts/check_ready.py --workspace $INVESTOR_WORKSPACE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
