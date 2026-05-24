"""Production-safety guards: prevent fixture / placeholder data from
escaping into real outreach.

A workspace scaffolded from `clients/test_workspace` or `init_workspace` may
contain `.example` domains, `{PLACEHOLDER}` strings the operator forgot to
edit, or `cal.example` scheduling links. Without these guards, Stage 7 can
emit `ready_to_send=TRUE` rows that route fictional data into Gmail drafts
or Attio sync.

Each helper returns a list of fail reasons; an empty list means the row /
payload is safe to publish. Callers decide whether to downgrade
(Stage 7 → outreach_status=draft) or refuse outright (Stage 8 sync, Gmail
draft creation).
"""
from __future__ import annotations

import re

# Curly-brace placeholders like {COMPANY_NAME}, {TIME_1}.
_PLACEHOLDER_RE = re.compile(r"\{[A-Z][A-Z0-9_]*\}")

# Reserved TLDs / domains for documentation per RFC 2606. Any of these in a
# production outreach artifact indicates fixture leakage.
_EXAMPLE_DOMAIN_SUFFIXES = (".example", ".test", ".invalid", ".localhost")
# Additional patterns commonly seen in init templates.
_EXAMPLE_DOMAIN_TOKENS = ("example.com", "example.org", "example.net")


def contains_placeholder(text: str | None) -> bool:
    """True if `text` contains a leftover `{TOKEN}` placeholder."""
    if not text:
        return False
    return bool(_PLACEHOLDER_RE.search(text))


def is_example_domain(domain: str | None) -> bool:
    """True if `domain` is a documentation/reserved domain (RFC 2606)
    or one of the common init-template tokens."""
    if not domain:
        return False
    d = domain.strip().lower()
    if any(d.endswith(suffix) for suffix in _EXAMPLE_DOMAIN_SUFFIXES):
        return True
    return any(token in d for token in _EXAMPLE_DOMAIN_TOKENS)


def is_example_email(email: str | None) -> bool:
    """True if `email` uses a documentation/reserved domain or is empty."""
    if not email:
        return False
    addr = email.strip().lower()
    if "@" not in addr:
        return False
    return is_example_domain(addr.split("@", 1)[1])


def production_gate_for_ready_to_send(
    *,
    subject: str | None,
    body: str | None,
    scheduling_link: str | None,
    founder_email: str | None,
    partner_email: str | None,
    allow_example_domains: bool = False,
) -> list[str]:
    """Reasons the row should NOT be marked ready_to_send. Empty = safe.

    Used by Stage 7 to downgrade `ready_to_send` to `draft` when the row
    carries fixture / placeholder data. Each reason is a short string the
    operator can act on.

    `allow_example_domains=True` skips ONLY the `.example`/.test/.invalid
    checks (used by fixture / smoke-test runs that legitimately exercise
    the ready-to-send path with RFC 2606 reserved domains). Placeholder
    checks (`{TOKEN}`, missing config) still fire either way.
    """
    fails: list[str] = []
    if contains_placeholder(subject):
        fails.append("subject contains unfilled placeholder")
    if contains_placeholder(body):
        fails.append("body contains unfilled placeholder")
    if not (scheduling_link or "").strip():
        fails.append("no scheduling link configured")
    elif contains_placeholder(scheduling_link):
        fails.append(
            f"scheduling link is a placeholder: {scheduling_link!r}"
        )
    elif (
        not allow_example_domains
        and is_example_domain(_url_host(scheduling_link))
    ):
        fails.append(
            f"scheduling link points at an example/reserved domain: "
            f"{scheduling_link!r}"
        )
    if not (founder_email or "").strip():
        fails.append("founder email not configured")
    elif contains_placeholder(founder_email):
        fails.append(f"founder email is a placeholder: {founder_email!r}")
    elif not allow_example_domains and is_example_email(founder_email):
        fails.append(
            f"founder email uses an example/reserved domain: {founder_email!r}"
        )
    if partner_email is not None:
        # partner_email is optional for ready_to_send (Stage 7's CSV is the
        # primary deliverable -- an operator may forward it). But if SET, it
        # must not be an .example address.
        if (
            partner_email.strip()
            and not allow_example_domains
            and is_example_email(partner_email)
        ):
            fails.append(
                f"partner email uses an example/reserved domain: "
                f"{partner_email!r}"
            )
    return fails


def production_gate_for_attio_sync(
    *,
    fund_domain: str | None,
    partner_email: str | None,
) -> list[str]:
    """Reasons Stage 8 should refuse to push this row to Attio."""
    fails: list[str] = []
    if fund_domain and is_example_domain(fund_domain):
        fails.append(
            f"fund_domain {fund_domain!r} is an example/reserved domain "
            f"(fixture data must not be synced to real Attio)"
        )
    if partner_email and is_example_email(partner_email):
        fails.append(
            f"partner email {partner_email!r} uses an example/reserved "
            f"domain"
        )
    return fails


def production_gate_for_gmail_draft(
    *,
    to_email: str | None,
    from_email: str | None,
    subject: str | None,
    body: str | None,
) -> list[str]:
    """Reasons create_gmail_drafts should refuse to push this draft."""
    fails: list[str] = []
    if not (to_email or "").strip():
        fails.append("recipient email missing")
    elif is_example_email(to_email):
        fails.append(
            f"recipient email {to_email!r} uses an example/reserved domain"
        )
    if from_email and is_example_email(from_email):
        fails.append(
            f"sender email {from_email!r} uses an example/reserved domain"
        )
    if contains_placeholder(subject):
        fails.append("subject contains unfilled placeholder")
    if contains_placeholder(body):
        fails.append("body contains unfilled placeholder")
    return fails


def _url_host(url: str | None) -> str | None:
    """Best-effort host extraction for safety checks. Avoid pulling urllib
    for one regex; we only need the part between scheme and the first
    `/` or `:`."""
    if not url:
        return None
    # Strip scheme.
    s = url.strip()
    if "://" in s:
        s = s.split("://", 1)[1]
    # Strip path, query, fragment, port.
    for sep in ("/", "?", "#", ":"):
        if sep in s:
            s = s.split(sep, 1)[0]
    return s or None
