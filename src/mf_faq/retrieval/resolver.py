"""Resolve a scheme and a field from a natural-language query.

Resolution order: exact name/slug → alias → fuzzy → session ``last_scheme``
(max three turns) → AMC-wide → clarify. Two named schemes always clarify.

See ``implementation.md`` §7.2 and §7.3.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Iterable

from mf_faq.chunking.taxonomy import match_field
from mf_faq.config import Settings

__all__ = [
    "SCHEME_ALIASES",
    "SCHEME_TITLES",
    "Resolution",
    "SchemeRef",
    "SchemeResolution",
    "resolve_query",
    "resolve_scheme",
]

SCHEME_ORDER: tuple[str, ...] = (
    "hdfc_large_cap",
    "hdfc_flexi_cap",
    "hdfc_elss",
    "hdfc_mid_cap",
    "amc_hdfc",
)

SCHEME_TITLES: dict[str, str] = {
    "hdfc_large_cap": "HDFC Large Cap Fund Direct Growth",
    "hdfc_flexi_cap": "HDFC Flexi Cap Direct Plan Growth",
    "hdfc_elss": "HDFC ELSS Tax Saver Fund Direct Plan Growth",
    "hdfc_mid_cap": "HDFC Mid Cap Fund Direct Growth",
    "amc_hdfc": "HDFC Mutual Fund",
}

SCHEME_ALIASES: dict[str, tuple[str, ...]] = {
    "hdfc_large_cap": (
        "hdfc large cap fund direct growth",
        "hdfc large cap direct growth",
        "hdfc-large-cap-fund-direct-growth",
        "hdfc large cap fund",
        "hdfc large cap",
        "large cap",
        "large-cap",
        "largecap",
    ),
    "hdfc_flexi_cap": (
        "hdfc equity fund direct growth",
        "hdfc-equity-fund-direct-growth",
        "hdfc flexi cap direct plan growth",
        "hdfc flexi cap fund",
        "hdfc flexi cap",
        "hdfc flexi",
        "hdfc equity",
        "flexi cap",
        "flexi-cap",
        "flexi",
    ),
    "hdfc_elss": (
        "hdfc elss tax saver fund direct plan growth",
        "hdfc-elss-tax-saver-fund-direct-plan-growth",
        "elss tax saver fund",
        "elss tax saver",
        "tax saver fund",
        "tax saving fund",
        "hdfc tax saver",
        "hdfc elss",
        "tax saver",
        "tax saving",
        "elss",
    ),
    "hdfc_mid_cap": (
        "hdfc mid cap fund direct growth",
        "hdfc-mid-cap-fund-direct-growth",
        "hdfc mid cap fund",
        "hdfc mid cap",
        "mid cap",
        "mid-cap",
        "midcap",
    ),
    "amc_hdfc": (
        "hdfc mutual fund",
        "hdfc amc",
        "hdfc-mutual-funds",
        "fund house",
        "capital gains statement",
        "capital gains",
        "statement download",
        "statement",
        "amc",
    ),
}

_NORMALISE_RE = re.compile(r"[._/\-–—]+")
_WHITESPACE_RE = re.compile(r"\s+")


def _normalise(text: str) -> str:
    text = (text or "").lower().replace("&", " and ")
    text = _NORMALISE_RE.sub(" ", text)
    return _WHITESPACE_RE.sub(" ", text).strip()


def _contains(haystack: str, needle: str) -> bool:
    if not needle:
        return False
    pattern = rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])"
    return re.search(pattern, haystack) is not None


def _alias_index() -> tuple[tuple[str, str, str], ...]:
    rows = [
        (_normalise(alias), scheme_id, alias)
        for scheme_id, aliases in SCHEME_ALIASES.items()
        for alias in aliases
    ]
    return tuple(sorted(rows, key=lambda row: (-len(row[0]), row[1], row[0])))


_ALIAS_INDEX: tuple[tuple[str, str, str], ...] = _alias_index()


@dataclass(frozen=True)
class SchemeRef:
    scheme_id: str
    doc_id: str
    title: str
    url: str
    page_type: str | None = None


@dataclass(frozen=True)
class SchemeResolution:
    scheme: SchemeRef | None
    ambiguous: tuple[SchemeRef, ...] = ()
    source: str = "none"
    matched_alias: str | None = None
    follow_up: bool = False

    @property
    def scheme_id(self) -> str | None:
        return self.scheme.scheme_id if self.scheme else None

    @property
    def needs_clarification(self) -> bool:
        return self.scheme is None


@dataclass(frozen=True)
class Resolution:
    scheme: SchemeRef | None
    field: str | None
    ambiguous: tuple[SchemeRef, ...] = ()
    source: str = "none"
    matched_alias: str | None = None
    follow_up: bool = False
    clarify_options: tuple[SchemeRef, ...] = ()

    @property
    def scheme_id(self) -> str | None:
        return self.scheme.scheme_id if self.scheme else None

    @property
    def doc_id(self) -> str | None:
        return self.scheme.doc_id if self.scheme else None

    @property
    def needs_clarification(self) -> bool:
        return self.scheme is None


def _catalogue(settings: Settings) -> dict[str, SchemeRef]:
    seeds = {seed.scheme_id: seed for seed in settings.corpus.seed_pages}
    catalogue: dict[str, SchemeRef] = {}
    for scheme_id in SCHEME_ORDER:
        seed = seeds.get(scheme_id)
        if seed is None:
            continue
        catalogue[scheme_id] = SchemeRef(
            scheme_id=scheme_id,
            doc_id=seed.id,
            title=SCHEME_TITLES.get(scheme_id, seed.scheme_id),
            url=seed.url,
            page_type=seed.page_type,
        )
    return catalogue


def _fuzzy_matches(normalised: str) -> list[tuple[str, str, str]]:
    """Return ``(alias, scheme_id, raw_alias)`` fuzzy matches."""
    tokens = normalised.split()
    if not tokens:
        return []
    matches: list[tuple[str, str, str]] = []
    for alias, scheme_id, raw in _ALIAS_INDEX:
        alias_tokens = alias.split()
        if len(alias_tokens) < 2:
            continue
        for size in {len(alias_tokens), len(alias_tokens) + 1}:
            if size > len(tokens):
                continue
            for start in range(len(tokens) - size + 1):
                window = " ".join(tokens[start : start + size])
                score = SequenceMatcher(None, window, alias).ratio()
                if score >= 0.86:
                    matches.append((alias, scheme_id, raw))
                    break
            else:
                continue
            break
    return matches


def _scheme_matches(query: str) -> list[tuple[str, str, str]]:
    normalised = _normalise(query)
    exact = [
        (alias, scheme_id, raw)
        for alias, scheme_id, raw in _ALIAS_INDEX
        if _contains(normalised, alias)
    ]
    if exact:
        return exact
    return _fuzzy_matches(normalised)


def _pick(
    matches: Iterable[tuple[str, str, str]],
    catalogue: dict[str, SchemeRef],
) -> tuple[SchemeRef | None, tuple[SchemeRef, ...], str | None]:
    best_by_scheme: dict[str, tuple[str, str]] = {}
    for alias, scheme_id, raw in matches:
        if scheme_id not in catalogue:
            continue
        current = best_by_scheme.get(scheme_id)
        if current is None or len(alias) > len(current[0]):
            best_by_scheme[scheme_id] = (alias, raw)

    if not best_by_scheme:
        return None, (), None

    # A named scheme wins over the AMC umbrella when both are mentioned.
    scheme_ids = [item for item in best_by_scheme if item != "amc_hdfc"]
    if not scheme_ids:
        scheme_ids = ["amc_hdfc"]

    ordered = [scheme_id for scheme_id in SCHEME_ORDER if scheme_id in scheme_ids]
    if len(ordered) > 1:
        return None, tuple(catalogue[item] for item in ordered), best_by_scheme[ordered[0]][1]
    scheme_id = ordered[0]
    return catalogue[scheme_id], (), best_by_scheme[scheme_id][1]


def resolve_scheme(
    query: str,
    *,
    settings: Settings,
    last_scheme: str | None = None,
    turn_count: int = 0,
) -> SchemeResolution:
    """Resolve only the scheme, including ambiguity and follow-up fallback."""
    catalogue = _catalogue(settings)
    scheme, ambiguous, matched_alias = _pick(_scheme_matches(query), catalogue)
    if ambiguous:
        return SchemeResolution(
            scheme=None,
            ambiguous=ambiguous,
            source="ambiguous",
            matched_alias=matched_alias,
        )
    if scheme is not None:
        return SchemeResolution(
            scheme=scheme,
            source="alias",
            matched_alias=matched_alias,
        )

    if last_scheme and turn_count < 3 and last_scheme in catalogue:
        return SchemeResolution(
            scheme=catalogue[last_scheme],
            source="last_scheme",
            follow_up=True,
        )

    if _normalise("amc") in _normalise(query).split() or "fund house" in _normalise(query):
        amc = catalogue.get("amc_hdfc")
        if amc is not None:
            return SchemeResolution(scheme=amc, source="amc_wide", matched_alias="amc")

    return SchemeResolution(scheme=None, source="none")


def _strip_scheme_alias(
    query: str,
    *,
    scheme_id: str | None,
    alias: str | None,
) -> str:
    """Remove the resolved scheme name so it cannot pose as a field synonym.

    "exit load on hdfc flexi cap" must resolve the field ``exit_load`` rather
    than ``category`` merely because "flexi cap" is also a category synonym.
    AMC aliases such as "statement" are kept because they are the only cue for
    the statement-download field.
    """
    if not alias or scheme_id == "amc_hdfc":
        return query
    normalized_query = _normalise(query)
    normalized_alias = _normalise(alias)
    if not normalized_alias:
        return query
    pattern = rf"(?<![a-z0-9]){re.escape(normalized_alias)}(?![a-z0-9])"
    stripped = re.sub(pattern, " ", normalized_query)
    return _WHITESPACE_RE.sub(" ", stripped).strip()


def resolve_query(
    query: str,
    *,
    settings: Settings,
    last_scheme: str | None = None,
    turn_count: int = 0,
) -> Resolution:
    """Resolve scheme and field, and decide whether the caller must clarify."""
    resolution = resolve_scheme(
        query,
        settings=settings,
        last_scheme=last_scheme,
        turn_count=turn_count,
    )
    field_query = _strip_scheme_alias(
        query,
        scheme_id=resolution.scheme_id,
        alias=resolution.matched_alias,
    )
    field = match_field(field_query)

    if resolution.ambiguous:
        return Resolution(
            scheme=None,
            field=field,
            ambiguous=resolution.ambiguous,
            source="ambiguous",
            matched_alias=resolution.matched_alias,
            clarify_options=resolution.ambiguous,
        )

    if resolution.scheme is None:
        options = tuple(
            _catalogue(settings)[scheme_id]
            for scheme_id in SCHEME_ORDER
            if scheme_id != "amc_hdfc" and scheme_id in _catalogue(settings)
        )
        return Resolution(
            scheme=None,
            field=field,
            source=resolution.source,
            clarify_options=options,
        )

    return Resolution(
        scheme=resolution.scheme,
        field=field,
        source=resolution.source,
        matched_alias=resolution.matched_alias,
        follow_up=resolution.follow_up,
    )
