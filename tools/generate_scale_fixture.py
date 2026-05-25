"""Generate a synthetic scale-test workspace: N funds, ~2N partners, ~2N
announcements, partner content for the first M partners.

This is mechanical-scale-test infrastructure for Sessions 10/11. The brief
expects real sources + a real LLM at this stage; in environments without
network or an API key, this generator proves the pipeline's mechanical
characteristics (memory, idempotency, ceiling enforcement, per-stage runtime)
without claiming the strategic insight a real scale-out provides.

Each fund cycles through 4 thesis templates:
  0: fintech infrastructure (matches Tendril target_sectors -> fund_adjacent)
  1: B2B SaaS / developer tools (partial overlap)
  2: pre-seed deep tech (stage mismatch -> disqualifier)
  3: growth software (growth-only -> disqualifier + major kill)

Run:
  uv run tools/generate_scale_fixture.py --n 50 --out /tmp/scale_50
"""
from __future__ import annotations

import argparse
import csv
import json
import pathlib
import shutil
from datetime import date, timedelta

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
TEMPLATE_WS = REPO_ROOT / "clients" / "test_workspace"

THESIS_VARIANTS = [
    ("Seed-stage fintech infrastructure for regulated markets.", "seed",
     "$500K-$2M", "fintech, infrastructure, regulated markets, compliance"),
    ("Seed B2B SaaS focused on developer tools and ops platforms.", "seed",
     "$1M-$3M", "B2B SaaS, developer tools, operations"),
    ("Pre-seed deep tech: materials and instrumentation.", "pre-seed",
     "$250K-$750K", "deep tech, hardware, materials"),
    ("Growth-stage capital for category-defining enterprise software.",
     "growth", "$15M-$50M", "enterprise software, growth"),
]

AXES_BY_VARIANT = {
    0: ["axis_1", "axis_2"],
    1: ["axis_3"],
    2: ["axis_2"],
    3: [],
}

PARTNER_SIGNAL_TEMPLATES = {
    "axis_1": (
        "Compliance reporting is the most underbuilt and most necessary layer "
        "in regulated fintech right now."
    ),
    "axis_2": (
        "We invest in the picks and shovels of infrastructure, not the "
        "application layer that depends on them."
    ),
    "axis_3": (
        "Five paying design partners doing real work beats a thousand sign-ups "
        "every time at seed."
    ),
    "axis_4": (
        "Policy and mandate windows create forced-buy timing that we "
        "underwrite explicitly."
    ),
}


def write_fund_pages(ws_path: pathlib.Path, fund_idx: int) -> None:
    """Write index/team/portfolio HTML for one fund."""
    variant = fund_idx % 4
    thesis, stage, check_size, sectors = THESIS_VARIANTS[variant]
    domain = f"fund{fund_idx}.example"
    fund_dir = ws_path / "data" / "fixtures" / "fund_pages" / domain
    fund_dir.mkdir(parents=True, exist_ok=True)

    kill_meta = ""
    if variant == 3:
        kill_meta = (
            '<meta name="kill-signal" content="We invest at Series B and '
            'later only.">'
        )

    (fund_dir / "index.html").write_text(
        f"""<!doctype html>
<html><head>
  <title>Fund {fund_idx} Capital</title>
  <meta name="thesis" content="{thesis}">
  <meta name="stage" content="{stage}">
  <meta name="check-size" content="{check_size}">
  <meta name="sectors" content="{sectors}">
  {kill_meta}
</head><body>
<h1>Fund {fund_idx} Capital</h1>
<p>{thesis}</p>
<p>We write {check_size} at {stage}.</p>
</body></html>
""",
        encoding="utf-8",
    )

    partner_a = f"Partner {2*fund_idx} A"
    partner_b = f"Partner {2*fund_idx + 1} B"
    (fund_dir / "team.html").write_text(
        f"""<!doctype html><html><head><title>Fund {fund_idx} Team</title></head>
<body><h1>Team</h1>
<div class="partner" data-name="{partner_a}" data-title="General Partner">
  General Partner at Fund {fund_idx}. Invests in {sectors.split(',')[0].strip()}.
</div>
<div class="partner" data-name="{partner_b}" data-title="Partner">
  Partner at Fund {fund_idx}.
</div>
</body></html>
""",
        encoding="utf-8",
    )

    (fund_dir / "portfolio.html").write_text(
        f"""<!doctype html><html><head><title>Fund {fund_idx} Portfolio</title></head>
<body><h1>Portfolio</h1><ul>
  <li class="portfolio-company">PortCo{fund_idx}-A</li>
  <li class="portfolio-company">PortCo{fund_idx}-B</li>
  <li class="portfolio-company">PortCo{fund_idx}-C</li>
</ul></body></html>
""",
        encoding="utf-8",
    )


def announcements_for(n_funds: int) -> list[dict]:
    """Two announcements per fund: one partner-attributed, one lead-only."""
    out: list[dict] = []
    base = date(2026, 5, 1)
    for i in range(n_funds):
        variant = i % 4
        _, stage, _, sectors_str = THESIS_VARIANTS[variant]
        tags = [t.strip() for t in sectors_str.split(",")]
        ann_date1 = (base - timedelta(days=10 + i)).isoformat()
        ann_date2 = (base - timedelta(days=40 + i)).isoformat()
        partner_a = f"Partner {2*i} A"
        fund_name = f"Fund {i} Capital"
        out.append({
            "source_url": f"https://news.example/portco{i}-a-{ann_date1}",
            "text": (
                f"PortCo{i}-A raised seed funding led by {fund_name}. "
                f"{partner_a} led the round."
            ),
            "_attribution": {
                "company": f"PortCo{i}-A",
                "round_type": stage if stage != "growth" else "Series B",
                "round_size_usd": 1_500_000 if variant != 3 else 25_000_000,
                "lead_investor": fund_name,
                "all_investors": [fund_name],
                "attributed_partners": [{"name": partner_a, "fund": fund_name}],
                "sector_tags": tags,
                "announcement_date": ann_date1,
            },
        })
        out.append({
            "source_url": f"https://news.example/portco{i}-b-{ann_date2}",
            "text": f"PortCo{i}-B closed a round led by {fund_name}.",
            "_attribution": {
                "company": f"PortCo{i}-B",
                "round_type": stage if stage != "growth" else "Series B",
                "round_size_usd": 1_000_000 if variant != 3 else 20_000_000,
                "lead_investor": fund_name,
                "all_investors": [fund_name],
                "attributed_partners": [],
                "sector_tags": tags,
                "announcement_date": ann_date2,
            },
        })
    return out


def partner_signals_for(n_partners: int) -> dict:
    """Generate one signal per partner for partner_signals_seed.json."""
    base = date(2026, 4, 1)
    out: dict = {}
    for i in range(n_partners):
        fund_idx = i // 2
        variant = fund_idx % 4
        axes = AXES_BY_VARIANT.get(variant, [])
        if not axes:
            continue  # growth funds get no thesis signals -> realistic gap
        axis = axes[i % len(axes)]
        quote = PARTNER_SIGNAL_TEMPLATES[axis]
        partner_letter = "A" if i % 2 == 0 else "B"
        partner_name = f"Partner {i} {partner_letter}"
        partner_id = f"fund{fund_idx}.example_partner_{i}_{partner_letter.lower()}"
        sig_date = (base - timedelta(days=(i * 3) % 200)).isoformat()
        out[partner_id] = {
            "sources": [{
                "source_type": "blog",
                "source_url": f"https://partner{i}.example/notes/{axis}",
                "quote_date": sig_date,
                "text": (
                    f"In a recent post {partner_name} wrote: '{quote}'"
                ),
            }],
            "_extraction": {
                "signals": [{
                    "quoted_text": quote,
                    "source_url": f"https://partner{i}.example/notes/{axis}",
                    "source_type": "blog",
                    "quote_date": sig_date,
                    "axis_relevance": [axis],
                    "signal_direction": "positive",
                    "confidence": "high",
                }],
                "reachability_signals": [{
                    "evidence": f"Recent blog post by {partner_name}.",
                    "source_url": f"https://partner{i}.example/notes/{axis}",
                    "direction": "positive",
                }],
                "cold_reachability_partial_score": 7.0,
                "cold_reachability_reasoning": "recent public output present",
            },
        }
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate scale-test workspace.")
    parser.add_argument("--n", type=int, required=True, help="Number of funds.")
    parser.add_argument("--out", required=True, help="Output workspace directory.")
    args = parser.parse_args()

    out_path = pathlib.Path(args.out).resolve()
    if out_path.exists():
        shutil.rmtree(out_path)
    out_path.mkdir(parents=True)

    # Copy config + minimal scaffolding from test_workspace.
    for sub in ("config", "prompts"):
        shutil.copytree(TEMPLATE_WS / sub, out_path / sub)
    (out_path / "data" / "fixtures" / "fund_pages").mkdir(parents=True)
    (out_path / "data" / "raw").mkdir(parents=True)
    (out_path / "exports").mkdir(parents=True)

    # Override sources.yaml to point at our generated CSV.
    (out_path / "config" / "sources.yaml").write_text(
        "public_lists:\n"
        "  - name: \"Scale fixture seed\"\n"
        "    path: \"data/fixtures/funds_seed.csv\"\n"
        "    parser: \"csv\"\n"
        "funding_announcement_feeds: []\n"
        "partner_signal_sources:\n"
        "  podcast_search_api: \"listennotes\"\n"
        "  substack_search: true\n",
        encoding="utf-8",
    )

    # funds_seed.csv
    with (out_path / "data" / "fixtures" / "funds_seed.csv").open(
        "w", newline="", encoding="utf-8"
    ) as fh:
        w = csv.writer(fh)
        w.writerow(["name", "domain"])
        for i in range(args.n):
            w.writerow([f"Fund {i} Capital", f"fund{i}.example"])

    # Fund pages
    for i in range(args.n):
        write_fund_pages(out_path, i)

    # announcements.json
    (out_path / "data" / "fixtures" / "announcements.json").write_text(
        json.dumps(announcements_for(args.n), indent=2), encoding="utf-8"
    )

    # partner_signals_seed.json
    (out_path / "data" / "fixtures" / "partner_signals_seed.json").write_text(
        json.dumps(partner_signals_for(args.n * 2), indent=2), encoding="utf-8"
    )

    print(
        f"[generate] wrote scale fixture at {out_path}: "
        f"{args.n} funds, ~{args.n * 2} partners, "
        f"{args.n * 2} announcements"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
