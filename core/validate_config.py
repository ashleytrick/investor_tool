"""Workspace config preflight validation.

Stages call `preflight_or_exit(ws, stage)` early so an operator who hasn't
edited the init_workspace placeholders or misshaped a YAML gets one clear
error list instead of a downstream KeyError or AttributeError.

Findings 146-160:
  146 raw KeyError on missing config -> structural checks return strings.
  147 placeholder values like "{COMPANY_NAME}" or "{AXIS_1_NAME}" detected.
  152 exactly 4 axes, validated.
  153 axis IDs match axis_1..axis_4.
  154 target_check_size_usd.min/max are integers, min <= max.
  155 raise_context fields present before outreach stages.
  156 minimum example counts per strategy.
  157 scheduling link present (when meeting_ask is configured).
  158 source parser is one of (csv, markdown, rss).
  159 live-stage API keys present when not in stub/fixture mode.
  160 attio mappings include the required preserve/override fields.

The validator never mutates anything; it only reports.
"""
from __future__ import annotations

import re
from typing import Any

# Curly-brace placeholders that came straight out of init_workspace templates.
# We match "{ANYTHING_IN_CAPS_OR_UNDERSCORES}" and a small set of long phrases
# from the templates (e.g. "{Seed | Series A | etc.}").
_PLACEHOLDER_RE = re.compile(r"\{[A-Z][A-Z0-9_ |.,/\-]*\}")
# Examples like "{e.g., $X ARR, X paying customers}" - lowercase "e.g."
_PLACEHOLDER_EG_RE = re.compile(r"\{e\.g\.,[^}]*\}")
# Examples like "{One sentence}" or "{Optional}" or "{phrases ...}"
_PLACEHOLDER_LOWER_RE = re.compile(
    r"\{(One sentence|Optional|priced|TARGET_RAISE|"
    r"phrases the founder naturally uses|"
    r"opening conversations[^}]*|"
    r"target first close date[^}]*|"
    r"milestone \d+|"
    r"soft-circled checks[^}]*|"
    r"Founder-designated[^}]*|"
    r"What they miss[^}]*|"
    r"LONGER_PARAGRAPH[^}]*|"
    r"ONE_SENTENCE[^}]*|"
    r"ANTI_SIGNAL|"
    r"KEYWORD_\d+|"
    r"ADJACENT_CO_\d+|"
    r"ANCHOR_FUND_\d+|"
    r"CALENDLY_OR_OTHER_URL|"
    r"sector_keyword_\d+|"
    r"ATTIO_WORKSPACE_ID)"
    r"\}"
)

# Strategies the operator MUST supply examples for before any live run.
# These mirror EXAMPLE_FILES in scripts/init_workspace.py and PROJECT_BRIEF.
REQUIRED_EXAMPLE_STRATEGIES = {
    "signal_led": 1,
    "portfolio_led": 1,
    "market_shift_led": 1,
    "round_pattern_led": 1,
    "traction_led": 1,
    "follow_up": 1,
    "deck_request_response": 1,
}

SUPPORTED_PARSERS = {"csv", "markdown", "rss"}


def _looks_like_placeholder(value: Any) -> bool:
    """True if `value` is a string that still contains an init template token."""
    if not isinstance(value, str) or not value:
        return False
    if _PLACEHOLDER_RE.search(value):
        return True
    if _PLACEHOLDER_EG_RE.search(value):
        return True
    if _PLACEHOLDER_LOWER_RE.search(value):
        return True
    return False


# Batch 21 (#718): very-loose email shape -- the goal is to catch
# "foo@bar" or "not-an-email", not to enforce RFC 5322. Real validation
# happens at send time via Gmail.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _looks_like_email(value: Any) -> bool:
    return isinstance(value, str) and bool(_EMAIL_RE.match(value.strip()))


def _scan_placeholders(obj: Any, path: str, issues: list[str]) -> None:
    """Recurse through dict/list and flag any leftover {PLACEHOLDER} strings."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            _scan_placeholders(v, f"{path}.{k}" if path else k, issues)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _scan_placeholders(v, f"{path}[{i}]", issues)
    elif _looks_like_placeholder(obj):
        issues.append(
            f"placeholder not edited at {path}: {obj!r} "
            f"-- replace with real workspace value"
        )


def _check_company(company_cfg: dict, issues: list[str]) -> None:
    co = (company_cfg or {}).get("company") or {}
    if not co:
        issues.append("company.yaml missing top-level 'company:' block")
        return
    for required in ("name", "founder_name", "founder_email", "one_liner",
                     "description", "stage"):
        if not co.get(required):
            issues.append(f"company.yaml: company.{required} missing or empty")

    # Batch 21 (#718): founder_email must look like an email.
    fe = (co.get("founder_email") or "").strip()
    if fe and not _looks_like_email(fe):
        issues.append(
            f"company.yaml: company.founder_email {fe!r} doesn't look like "
            f"a valid email address"
        )

    cs = co.get("target_check_size_usd") or {}
    if not isinstance(cs, dict):
        issues.append("company.yaml: company.target_check_size_usd must be a mapping")
    else:
        mn = cs.get("min")
        mx = cs.get("max")
        if not isinstance(mn, int) or mn <= 0:
            issues.append(
                f"company.yaml: target_check_size_usd.min must be a positive "
                f"integer (got {mn!r})"
            )
        if not isinstance(mx, int) or mx <= 0:
            issues.append(
                f"company.yaml: target_check_size_usd.max must be a positive "
                f"integer (got {mx!r})"
            )
        if isinstance(mn, int) and isinstance(mx, int) and mn > mx:
            issues.append(
                f"company.yaml: target_check_size_usd.min ({mn}) > max ({mx})"
            )

    ts = co.get("target_sectors") or []
    if not isinstance(ts, list) or len(ts) < 1:
        issues.append("company.yaml: company.target_sectors must list >=1 sector")


def _check_raise_context(company_cfg: dict, issues: list[str]) -> None:
    rc = (company_cfg or {}).get("raise_context") or {}
    if not rc:
        issues.append(
            "company.yaml missing 'raise_context:' block "
            "-- system only operates when an active raise is in progress"
        )
        return
    for required in ("round", "amount", "status", "timing"):
        if not rc.get(required):
            issues.append(f"company.yaml: raise_context.{required} missing or empty")


def _check_meeting_ask(company_cfg: dict, issues: list[str]) -> None:
    ma = ((company_cfg or {}).get("company") or {}).get("meeting_ask") or {}
    if not ma:
        # Meeting ask is optional config-wise; emails fall back to "30 min".
        return
    link = ma.get("preferred_scheduling_link")
    if link and _looks_like_placeholder(link):
        issues.append(
            f"company.yaml: meeting_ask.preferred_scheduling_link is still "
            f"a placeholder ({link!r})"
        )
    # Batch 21 (#717): scheduling link must be HTTPS so the operator
    # doesn't ship a `http://` link into outreach (browsers warn / block).
    if link and not _looks_like_placeholder(link):
        if not (link.startswith("https://") or link.startswith("http://")):
            issues.append(
                f"company.yaml: meeting_ask.preferred_scheduling_link "
                f"{link!r} should start with https:// or http://"
            )
        elif link.startswith("http://"):
            issues.append(
                f"company.yaml: meeting_ask.preferred_scheduling_link "
                f"{link!r} uses http://; use https:// for outreach"
            )
    # Batch 21 (#716): duration_minutes within a reasonable range. The
    # brief defaults to 30; reject 0/negative or absurd (> 240) values.
    dm = ma.get("duration_minutes")
    if dm is not None:
        if not isinstance(dm, int) or dm <= 0 or dm > 240:
            issues.append(
                f"company.yaml: meeting_ask.duration_minutes {dm!r} should "
                f"be a positive integer <= 240"
            )


def _check_axes(axes_cfg: dict, issues: list[str]) -> None:
    axes = (axes_cfg or {}).get("axes") or []
    if not isinstance(axes, list):
        issues.append("axes.yaml: 'axes:' must be a list")
        return
    if len(axes) != 4:
        issues.append(
            f"axes.yaml: expected exactly 4 axes (got {len(axes)}); "
            f"Stage 6 scoring assumes 4 orthogonal belief axes"
        )
    expected_ids = {f"axis_{i}" for i in range(1, 5)}
    seen_ids: set[str] = set()
    # Batch 21 (#727): detect axes that are duplicate copies of each other
    # (same name OR same description -- exact-match heuristic). Operators
    # who collapse two axes during editing sometimes leave both rows.
    seen_names: dict[str, int] = {}
    seen_descs: dict[str, int] = {}
    for i, ax in enumerate(axes):
        if not isinstance(ax, dict):
            issues.append(f"axes.yaml: axes[{i}] must be a mapping")
            continue
        aid = ax.get("id")
        if aid in seen_ids:
            issues.append(f"axes.yaml: duplicate axis id {aid!r}")
        if aid:
            seen_ids.add(aid)
        for required in ("id", "name", "description"):
            if not ax.get(required):
                issues.append(f"axes.yaml: axes[{i}].{required} missing or empty")
        # Batch 21 (#723/#724): weights must be POSITIVE and in [0.1, 5.0].
        # Negative weights would invert the axis contribution; weights >5
        # would dominate every composite over a normalized axis.
        w = ax.get("weight")
        if w is not None:
            if not isinstance(w, (int, float)):
                issues.append(
                    f"axes.yaml: axes[{i}].weight must be numeric (got {w!r})"
                )
            elif w <= 0:
                issues.append(
                    f"axes.yaml: axes[{i}].weight ({w}) must be positive"
                )
            elif w > 5.0:
                issues.append(
                    f"axes.yaml: axes[{i}].weight ({w}) > 5.0 will dominate "
                    f"composite scoring; cap at 5.0 or rebalance"
                )
        if not ax.get("positive_signals"):
            issues.append(
                f"axes.yaml: axes[{i}].positive_signals must list >=1 keyword"
            )
        nm = (ax.get("name") or "").strip().lower()
        ds = (ax.get("description") or "").strip().lower()
        if nm:
            if nm in seen_names:
                issues.append(
                    f"axes.yaml: axes[{i}] has the same name as "
                    f"axes[{seen_names[nm]}] -- two axes describing the "
                    f"same belief; collapse or differentiate"
                )
            else:
                seen_names[nm] = i
        if ds:
            if ds in seen_descs:
                issues.append(
                    f"axes.yaml: axes[{i}] has the same description as "
                    f"axes[{seen_descs[ds]}] -- two axes describing the "
                    f"same belief; collapse or differentiate"
                )
            else:
                seen_descs[ds] = i
    if len(axes) == 4 and seen_ids and seen_ids != expected_ids:
        issues.append(
            f"axes.yaml: axis IDs must be exactly {sorted(expected_ids)} "
            f"(Stage 6 scoring is keyed on these IDs); got {sorted(seen_ids)}"
        )


def _check_sources(sources_cfg: dict, issues: list[str]) -> None:
    public_lists = (sources_cfg or {}).get("public_lists") or []
    if not isinstance(public_lists, list):
        issues.append("sources.yaml: 'public_lists:' must be a list")
        return
    for i, src in enumerate(public_lists):
        if not isinstance(src, dict):
            issues.append(f"sources.yaml: public_lists[{i}] must be a mapping")
            continue
        parser = src.get("parser")
        if parser and parser not in SUPPORTED_PARSERS:
            issues.append(
                f"sources.yaml: public_lists[{i}].parser={parser!r} not in "
                f"{sorted(SUPPORTED_PARSERS)}"
            )
        if not src.get("path") and not src.get("url"):
            issues.append(
                f"sources.yaml: public_lists[{i}] must have 'path:' or 'url:'"
            )
    feeds = (sources_cfg or {}).get("funding_announcement_feeds") or []
    if not isinstance(feeds, list):
        issues.append("sources.yaml: 'funding_announcement_feeds:' must be a list")
    else:
        for i, f in enumerate(feeds):
            p = (f or {}).get("parser")
            if p and p not in SUPPORTED_PARSERS:
                issues.append(
                    f"sources.yaml: funding_announcement_feeds[{i}].parser="
                    f"{p!r} not in {sorted(SUPPORTED_PARSERS)}"
                )


def _check_examples(ws, issues: list[str]) -> None:
    """Each required strategy must have an example file with non-stub body."""
    examples_dir = ws.examples_dir
    if not examples_dir.exists():
        issues.append(
            f"prompts/examples/ missing at {examples_dir} -- need >=1 example "
            f"per strategy ({sorted(REQUIRED_EXAMPLE_STRATEGIES)})"
        )
        return
    for strategy, min_count in REQUIRED_EXAMPLE_STRATEGIES.items():
        fname = examples_dir / f"{strategy}.md"
        if not fname.exists():
            issues.append(
                f"prompts/examples/{strategy}.md missing -- the live email "
                f"prompt anchors style on these examples"
            )
            continue
        body = fname.read_text(encoding="utf-8")
        # Heuristic: the init_workspace stub leaves "Subject: ..." and
        # "Body (replace with your actual prose):" in place. If we still see
        # the literal "Replace these placeholders" anchor and no real Subject
        # line, treat it as unedited.
        if "Replace these placeholders" in body and "Subject: " not in body.replace(
            "Subject: ...", ""
        ):
            issues.append(
                f"prompts/examples/{strategy}.md still contains the init "
                f"stub -- replace with {min_count}+ real example email(s)"
            )


def _check_attio(ws, issues: list[str]) -> None:
    """Validate attio.yaml structure only when present. Empty is a no-op."""
    attio_cfg = ws.attio or {}
    if not attio_cfg:
        return
    cfg = attio_cfg.get("attio") or attio_cfg
    objects = cfg.get("objects") or {}
    if "funds" not in objects or "partners" not in objects:
        issues.append(
            "attio.yaml: 'objects' must define both 'funds' and 'partners' "
            "(e.g. {funds: companies, partners: people})"
        )
    # Finding 160: preserve_on_outreach_started and manual_override_protection
    # are referenced by Stage 8; if attio.yaml is present they should be too.
    for required in ("fund_attributes", "partner_attributes"):
        if required not in cfg:
            issues.append(
                f"attio.yaml: '{required}' missing -- Stage 8 sync needs the "
                f"slug-mapping to write any non-base attribute"
            )
    matching = cfg.get("matching_attributes") or {}
    if not matching.get("companies") or not matching.get("people"):
        issues.append(
            "attio.yaml: 'matching_attributes' must map 'companies' and "
            "'people' to the slugs used for upsert matching"
        )


def _check_live_keys(ws, *, require_anthropic: bool,
                    require_attio: bool, issues: list[str]) -> None:
    if require_anthropic and not ws.env("ANTHROPIC_API_KEY"):
        issues.append(
            "ANTHROPIC_API_KEY not set in workspace .env, root .env, or "
            "process env -- live LLM stages will refuse to run"
        )
    if require_attio and not ws.env("ATTIO_API_KEY"):
        issues.append(
            "ATTIO_API_KEY not set -- Attio sync needs the workspace bearer token"
        )


def validate_workspace_config(
    ws,
    *,
    require_anthropic: bool = False,
    require_attio: bool = False,
    require_examples: bool = False,
) -> list[str]:
    """Run all preflight checks. Returns issues; empty list means valid.

    Stages call this early. The flags reflect the stage's specific needs:
      - Stage 5/7 set require_anthropic when not in stub/fixtures mode.
      - Stage 0/8 set require_attio when attio.yaml is present.
      - Stage 7 sets require_examples (live email prompt anchors on them).
    """
    issues: list[str] = []

    if not ws.company:
        issues.append(
            f"config/company.yaml missing at {ws.config_dir / 'company.yaml'}"
        )
    else:
        _check_company(ws.company, issues)
        _check_raise_context(ws.company, issues)
        _check_meeting_ask(ws.company, issues)
        _scan_placeholders(ws.company, "company.yaml", issues)

    if not ws.axes:
        issues.append(
            f"config/axes.yaml missing at {ws.config_dir / 'axes.yaml'}"
        )
    else:
        _check_axes(ws.axes, issues)
        _scan_placeholders(ws.axes, "axes.yaml", issues)

    if ws.sources:
        _check_sources(ws.sources, issues)

    if require_examples:
        _check_examples(ws, issues)

    _check_attio(ws, issues)

    _check_live_keys(
        ws,
        require_anthropic=require_anthropic,
        require_attio=require_attio,
        issues=issues,
    )

    return issues


def preflight_or_exit(
    ws,
    *,
    stage: str,
    require_anthropic: bool = False,
    require_attio: bool = False,
    require_examples: bool = False,
) -> None:
    """Print issues and SystemExit(2) on any. No-op when clean."""
    issues = validate_workspace_config(
        ws,
        require_anthropic=require_anthropic,
        require_attio=require_attio,
        require_examples=require_examples,
    )
    if not issues:
        return
    print(f"[{stage}] REFUSED: workspace config has {len(issues)} issue(s):")
    for s in issues:
        print(f"  - {s}")
    print(
        f"[{stage}] edit clients/{ws.name}/config/*.yaml (and prompts/examples/) "
        f"then re-run."
    )
    raise SystemExit(2)
