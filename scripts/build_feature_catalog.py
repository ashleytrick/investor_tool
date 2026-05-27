"""Generate docs/FEATURE_CATALOG.md from the live FastAPI app.

For every HTTP route registered on the app, looks up:
  - HTTP method + path
  - The handler function and its module
  - Whether any test file mentions the path (rough proxy for coverage)
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# Avoid blocking imports.
os.environ.setdefault("API_KEY", "catalog")
os.environ.setdefault("CORS_ORIGINS", "https://example.com")
os.environ.setdefault("INVESTOR_WORKSPACE", str(REPO / "clients" / "test_workspace"))

from web.api import app  # noqa: E402


def grep_tests_for_path(
    tests_dir: Path, path: str, handler_name: str,
) -> list[str]:
    """Return test files that plausibly cover this endpoint.

    Three signals (any one counts as a hit):
      1. The path with `{param}` placeholders replaced by `.+`
         appears in the test (regex match) — catches f-string call
         sites like `f"/drafts/{draft_id}/mark-sent"`.
      2. The path prefix (everything up to the first `{`) appears
         literally — catches partial-path mentions.
      3. The handler function name appears in the test — catches
         tests that import + call the handler directly.
    """
    # 1. Regex on the parametrized path.
    regex = re.compile(re.sub(r"\{[^}]+\}", r"[^\"/' ]+", path))
    # 2. Path prefix (before any `{`).
    prefix_split = path.split("{", 1)[0].rstrip("/")
    prefix_needle = (
        prefix_split if len(prefix_split) >= 5 else None
    )
    matches: list[str] = []
    for test in sorted(tests_dir.glob("test_*.py")):
        try:
            text = test.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        hit = (
            regex.search(text) is not None
            or (prefix_needle and prefix_needle in text)
            or (handler_name and handler_name in text)
        )
        if hit:
            matches.append(test.name)
    return matches


def main() -> None:
    tests_dir = REPO / "tests"

    # Group routes by tag (FastAPI's tag system) or by first path segment.
    rows = []
    for route in app.routes:
        if not hasattr(route, "methods"):
            continue
        for m in route.methods:
            if m == "HEAD":
                continue
            path = route.path
            tags = getattr(route, "tags", []) or []
            handler = getattr(route, "endpoint", None)
            handler_name = getattr(handler, "__name__", "<?>")
            handler_module = getattr(handler, "__module__", "<?>")
            tests = grep_tests_for_path(tests_dir, path, handler_name)
            rows.append({
                "method": m,
                "path": path,
                "tag": tags[0] if tags else "(untagged)",
                "handler": f"{handler_module}.{handler_name}",
                "tests": tests,
            })

    rows.sort(key=lambda r: (r["tag"], r["path"], r["method"]))

    # Group by tag.
    by_tag: dict[str, list] = {}
    for r in rows:
        by_tag.setdefault(r["tag"], []).append(r)

    out = ["# Feature Catalog (HTTP API surface)\n"]
    out.append(f"_Generated from `web.api.app`. {len(rows)} HTTP endpoints across {len(by_tag)} tags._\n")
    out.append("Each row maps an endpoint to the test file(s) that mention its path. **No test files** under `tests/` ⇒ flagged as gap.\n\n")
    total_covered = sum(1 for r in rows if r["tests"])
    out.append(f"**Coverage at endpoint granularity: {total_covered}/{len(rows)} endpoints have at least one test file mentioning their path.**\n\n")
    for tag in sorted(by_tag.keys()):
        tag_rows = by_tag[tag]
        out.append(f"## {tag} ({len(tag_rows)})\n")
        out.append("| Method | Path | Handler | Tests |")
        out.append("|---|---|---|---|")
        for r in tag_rows:
            tests_str = ", ".join(r["tests"][:3])
            if len(r["tests"]) > 3:
                tests_str += f" (+{len(r['tests']) - 3})"
            if not tests_str:
                tests_str = "**❌ no test mentions this path**"
            out.append(
                f"| `{r['method']}` | `{r['path']}` | `{r['handler']}` | {tests_str} |"
            )
        out.append("")

    target = REPO / "docs" / "FEATURE_CATALOG.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(out), encoding="utf-8")
    print(f"Wrote {target} ({len(rows)} routes, {total_covered} with test mention)")


if __name__ == "__main__":
    main()
