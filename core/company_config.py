"""Typed read-only view over a parsed `company.yaml` workspace config.

The workspace YAML has a stable top-level shape:

    mode: fixture | dry_run | production
    company:           {...}
    raise_context:     {...}
    founder_voice:     {...}
    round_fit:         {...}

`CompanyConfig.from_dict(...)` wraps that dict in typed accessors so
callers stop repeating

    c = (company_cfg or {}).get("company") or {}
    rc = (company_cfg or {}).get("raise_context") or {}
    problem = c.get("problem") or ""

at every site. Each accessor returns a sensible default (``""`` for
text, ``[]`` for lists, ``None`` for nullable ints) so a half-filled
config still produces a usable view -- the caller doesn't need a
defensive ``or {}`` for every section.

The original dict is preserved as ``.raw`` so code paths that already
take ``company_cfg: dict`` keep working during the migration. Read
only by convention -- mutations go through the API's PUT
/config/company route, which writes the YAML directly.
"""
from __future__ import annotations

from typing import Any


class _Section:
    """Shared typed-getter base for the per-block views below."""

    __slots__ = ("_d",)

    def __init__(self, d: dict | None) -> None:
        self._d = d or {}

    def _str(self, key: str, default: str = "") -> str:
        v = self._d.get(key)
        return v if isinstance(v, str) else default

    def _int(self, key: str) -> int | None:
        v = self._d.get(key)
        # bool is a subclass of int; explicitly reject so a stray
        # `True` value doesn't get coerced to 1.
        if isinstance(v, bool):
            return None
        return v if isinstance(v, int) else None

    def _list(self, key: str) -> list[str]:
        v = self._d.get(key)
        if isinstance(v, list):
            return [str(x) for x in v]
        return []

    @property
    def raw(self) -> dict:
        """The underlying dict. Useful for code paths that still
        take a dict (e.g. while migration is in progress) or for
        passing the section to a JSON serializer."""
        return self._d


class CompanyView(_Section):
    """The `company:` block of company.yaml."""

    # Identity.
    @property
    def name(self) -> str: return self._str("name")
    @property
    def one_liner(self) -> str: return self._str("one_liner")
    @property
    def website(self) -> str: return self._str("website")
    @property
    def founded_year(self) -> int | None: return self._int("founded_year")
    @property
    def hq_location(self) -> str: return self._str("hq_location")

    # Pitch.
    @property
    def stage(self) -> str: return self._str("stage")
    @property
    def sectors(self) -> list[str]: return self._list("sectors")
    @property
    def business_model(self) -> str: return self._str("business_model")
    @property
    def problem(self) -> str: return self._str("problem")
    @property
    def solution(self) -> str: return self._str("solution")
    @property
    def differentiators(self) -> str: return self._str("differentiators")
    @property
    def why_now(self) -> str: return self._str("why_now")
    @property
    def traction(self) -> str:
        # Prefer the flat `traction` field added by the onboarding
        # wizard; fall back to legacy nested current_traction.headline_metric
        # so workspaces edited via the CLI still surface data.
        flat = self._str("traction")
        if flat:
            return flat
        nested = self._d.get("current_traction")
        if isinstance(nested, dict):
            head = nested.get("headline_metric")
            if isinstance(head, str):
                return head
        return ""

    # Round.
    @property
    def round_amount_usd(self) -> int | None:
        return self._int("round_amount_usd")
    @property
    def round_instrument(self) -> str: return self._str("round_instrument")
    @property
    def round_valuation_usd(self) -> int | None:
        return self._int("round_valuation_usd")
    @property
    def round_close_target(self) -> str:
        return self._str("round_close_target")

    # Investor fit.
    @property
    def target_check_min_usd(self) -> int | None:
        # Flat field with fallback to the legacy nested shape.
        flat = self._int("target_check_min_usd")
        if flat is not None:
            return flat
        nested = self._d.get("target_check_size_usd")
        if isinstance(nested, dict):
            v = nested.get("min")
            if isinstance(v, int) and not isinstance(v, bool):
                return v
        return None

    @property
    def target_check_max_usd(self) -> int | None:
        flat = self._int("target_check_max_usd")
        if flat is not None:
            return flat
        nested = self._d.get("target_check_size_usd")
        if isinstance(nested, dict):
            v = nested.get("max")
            if isinstance(v, int) and not isinstance(v, bool):
                return v
        return None

    @property
    def target_stages(self) -> list[str]: return self._list("target_stages")
    @property
    def target_sectors(self) -> list[str]:
        return self._list("target_sectors")
    @property
    def target_geographies(self) -> list[str]:
        return self._list("target_geographies")
    @property
    def desired_traits(self) -> list[str]:
        return self._list("desired_traits")

    # Anti-criteria.
    @property
    def excluded_sectors(self) -> list[str]:
        return self._list("excluded_sectors")
    @property
    def excluded_geographies(self) -> list[str]:
        return self._list("excluded_geographies")
    @property
    def do_not_contact(self) -> list[str]:
        return self._list("do_not_contact")

    # Founder + outreach.
    @property
    def founder_name(self) -> str: return self._str("founder_name")
    @property
    def founder_title(self) -> str: return self._str("founder_title")
    @property
    def founder_email(self) -> str: return self._str("founder_email")
    @property
    def signature(self) -> str: return self._str("signature")
    @property
    def tone(self) -> str: return self._str("tone")
    @property
    def scheduling_link(self) -> str:
        flat = self._str("scheduling_link")
        if flat:
            return flat
        nested = self._d.get("meeting_ask")
        if isinstance(nested, dict):
            v = nested.get("preferred_scheduling_link")
            if isinstance(v, str):
                return v
        return ""


class RaiseContextView(_Section):
    """The `raise_context:` block of company.yaml."""

    @property
    def round(self) -> str: return self._str("round")
    @property
    def amount(self) -> str: return self._str("amount")
    @property
    def instrument(self) -> str: return self._str("instrument")
    @property
    def timing(self) -> str: return self._str("timing")
    @property
    def status(self) -> str: return self._str("status")
    @property
    def strongest_raise_proof(self) -> str:
        return self._str("strongest_raise_proof")
    @property
    def why_this_round_is_fundable_now(self) -> str:
        return self._str("why_this_round_is_fundable_now")
    @property
    def what_changes_after_this_round(self) -> str:
        return self._str("what_changes_after_this_round")
    @property
    def notable_existing_investors_or_non_dilutive(self) -> str:
        return self._str("notable_existing_investors_or_non_dilutive")


class FounderVoiceView(_Section):
    """The `founder_voice:` block (controls email tone + banned phrases)."""

    @property
    def style(self) -> str: return self._str("style")
    @property
    def banned_phrases(self) -> list[str]:
        return self._list("banned_phrases")
    @property
    def preferred_phrases(self) -> list[str]:
        return self._list("preferred_phrases")


class RoundFitView(_Section):
    """The `round_fit:` block (deterministic scoring rules)."""

    @property
    def must_have(self) -> list[str]: return self._list("must_have")
    @property
    def nice_to_have(self) -> list[str]: return self._list("nice_to_have")
    @property
    def disqualifiers(self) -> list[str]:
        return self._list("disqualifiers")


class CompanyConfig:
    """Typed view of the full workspace company.yaml.

    Usage:

        cfg = CompanyConfig.from_dict(ws.company)
        if cfg.company.problem:
            ...
        for d in cfg.round_fit.disqualifiers:
            ...

    Sections are eagerly constructed in ``__init__`` so repeated
    access is free; the underlying dicts are not copied (each view
    holds a reference, mutations to the source dict are visible to
    the view).
    """

    __slots__ = ("raw", "company", "raise_context", "founder_voice", "round_fit")

    def __init__(self, raw: dict | None) -> None:
        self.raw: dict = raw or {}
        self.company = CompanyView(self.raw.get("company"))
        self.raise_context = RaiseContextView(self.raw.get("raise_context"))
        self.founder_voice = FounderVoiceView(self.raw.get("founder_voice"))
        self.round_fit = RoundFitView(self.raw.get("round_fit"))

    @classmethod
    def from_dict(cls, raw: dict | None) -> "CompanyConfig":
        """Build a CompanyConfig from a parsed YAML dict. Equivalent
        to ``CompanyConfig(raw)`` but reads as the explicit
        constructor pattern at call sites."""
        return cls(raw)

    @property
    def mode(self) -> str:
        """Workspace mode -- fixture / dry_run / production. The
        config_loader normalizes legacy aliases (prod, dev) before
        this is reached, so the value here is always one of the
        canonical three (or "dry_run" by default on absence)."""
        m = self.raw.get("mode")
        return m if isinstance(m, str) else "dry_run"
