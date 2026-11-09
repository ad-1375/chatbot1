"""Canonical field names and longest-synonym matching.

The chunker uses this module to map a label, a fact, or a section heading to a
stable field name. Synonyms are normalised (case, punctuation, hyphens) and
matched longest-first so ``exit`` never overrides ``exit load``.

See implementation.md §4.2.
"""

from __future__ import annotations

import re
from typing import Iterable

CANONICAL_FIELDS: tuple[str, ...] = (
    "expense_ratio",
    "exit_load",
    "minimum_sip",
    "lock_in",
    "riskometer",
    "benchmark",
    "statement_download",
    "fund_size",
    "category",
    "plan",
    "option",
    "fund_manager",
    "launch_date",
    "other",
)

FIELD_DISPLAY: dict[str, str] = {
    "expense_ratio": "Expense ratio",
    "exit_load": "Exit load",
    "minimum_sip": "Minimum SIP",
    "lock_in": "Lock-in",
    "riskometer": "Riskometer",
    "benchmark": "Benchmark",
    "statement_download": "Statement download",
    "fund_size": "Fund size",
    "category": "Category",
    "plan": "Plan",
    "option": "Option",
    "fund_manager": "Fund manager",
    "launch_date": "Launch date",
    "other": "Fact",
}

# Synonyms are written in normal form (lower case, punctuation and hyphens
# already reduced to single spaces). They are normalised again defensively.
FIELD_SYNONYMS: dict[str, tuple[str, ...]] = {
    "expense_ratio": (
        "total expense ratio",
        "expense ratio",
        "expense",
        "ter",
    ),
    "exit_load": (
        "exit load",
        "exit",
    ),
    "minimum_sip": (
        "minimum sip investment",
        "minimum investment",
        "min for 1st investment",
        "min for 2nd investment",
        "min investment",
        "minimum sip",
        "min for sip",
        "min sip",
        "min for",
    ),
    "lock_in": (
        "lock in period",
        "lock in",
        "lockin",
    ),
    "riskometer": (
        "risk o meter",
        "riskometer",
        "risk",
    ),
    "benchmark": (
        "fund benchmark",
        "scheme benchmark",
        "tier 1 benchmark",
        "benchmark",
    ),
    "statement_download": (
        "capital gains statement",
        "statement download",
        "download statement",
        "account statement",
        "capital gains",
        "statement",
    ),
    "fund_size": (
        "assets under management",
        "fund size",
        "total aum",
        "aum",
        "corpus",
    ),
    "category": (
        "asset class",
        "fund category",
        "scheme category",
        "mutual fund category",
        "fund type",
        "scheme type",
        "large cap",
        "mid cap",
        "small cap",
        "flexi cap",
        "category",
        "equity",
        "debt",
        "hybrid",
        "elss",
    ),
    "plan": (
        "regular plan",
        "direct plan",
        "plan",
    ),
    "option": (
        "growth option",
        "idcw option",
        "idcw",
        "option",
    ),
    "fund_manager": (
        "fund management",
        "fund manager",
        "managed by",
        "manager",
    ),
    "launch_date": (
        "date of incorporation",
        "incorporation date",
        "allotment date",
        "inception date",
        "launch date",
        "launched on",
        "inception",
    ),
}

_NORMALISE_RE = re.compile(r"[._/\-–—]+")
_WHITESPACE_RE = re.compile(r"\s+")

_NORMALISE_RE = re.compile(r"[._/\-–—]+")
_WHITESPACE_RE = re.compile(r"\s+")


def _normalise(text: str) -> str:
    text = (text or "").lower().replace("&", " and ")
    text = _NORMALISE_RE.sub(" ", text)
    return _WHITESPACE_RE.sub(" ", text).strip()


# Longest synonym wins globally; ties keep dictionary order for determinism.
_SYNONYM_INDEX: tuple[tuple[str, str], ...] = tuple(
    sorted(
        (
            (_normalise(synonym), field)
            for field, synonyms in FIELD_SYNONYMS.items()
            for synonym in synonyms
        ),
        key=lambda item: (-len(item[0]), item[1], item[0]),
    )
)


def _contains(haystack: str, needle: str) -> bool:
    if not needle:
        return False
    pattern = rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])"
    return re.search(pattern, haystack) is not None


def match_field(text: str) -> str | None:
    """Return the first (longest) canonical field found in ``text``."""
    normalised = _normalise(text)
    if not normalised:
        return None
    for synonym, field in _SYNONYM_INDEX:
        if _contains(normalised, synonym):
            return field
    return None


def resolve_field(
    *,
    label: str | None = None,
    text: str | None = None,
    header: Iterable[str] | None = None,
    section_path: Iterable[str] | None = None,
) -> str:
    """Resolve the canonical field for one block.

    A block that carries its own label is matched on that label only. Falling
    back to the section there would turn ``Stamp duty on investment`` into
    ``exit_load`` just because it sits under the exit-load heading.
    """
    if label and label.strip():
        return match_field(label) or "other"

    candidates: list[str] = []
    if text:
        candidates.append(text)
    if header:
        candidates.extend(header)
    if section_path:
        path = list(section_path)
        if path:
            candidates.append(path[-1])

    for candidate in candidates:
        field = match_field(candidate)
        if field:
            return field
    return "other"


def field_display(field: str) -> str:
    return FIELD_DISPLAY.get(field, FIELD_DISPLAY["other"])


def synonyms_for(field: str) -> tuple[str, ...]:
    """Expose the raw synonym list (used by tests and documentation)."""
    return FIELD_SYNONYMS.get(field, ())
