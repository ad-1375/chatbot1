"""Scheme and field resolution tests (§7.2, §7.3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from mf_faq.config import Settings
from mf_faq.retrieval.resolver import SCHEME_ALIASES, resolve_query, resolve_scheme

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "config" / "config.yaml"


@pytest.fixture(scope="module")
def settings() -> Settings:
    return Settings.load(CONFIG)


@pytest.mark.parametrize(
    ("scheme_id", "alias"),
    [
        (scheme_id, alias)
        for scheme_id, aliases in SCHEME_ALIASES.items()
        for alias in aliases
    ],
)
def test_every_alias_resolves(
    settings: Settings, scheme_id: str, alias: str
) -> None:
    resolution = resolve_scheme(alias, settings=settings)

    assert resolution.scheme_id == scheme_id, alias


def test_legacy_flexi_slug_resolves(settings: Settings) -> None:
    resolution = resolve_query(
        "legacy flexi slug hdfc-equity-fund-direct-growth", settings=settings
    )

    assert resolution.scheme_id == "hdfc_flexi_cap"
    assert resolution.field is None


def test_elss_lock_in_resolves_scheme_and_field(settings: Settings) -> None:
    resolution = resolve_query("ELSS lock-in", settings=settings)

    assert resolution.scheme_id == "hdfc_elss"
    assert resolution.field == "lock_in"


def test_scheme_name_does_not_override_explicit_field(settings: Settings) -> None:
    resolution = resolve_query("exit load on hdfc flexi cap", settings=settings)

    assert resolution.scheme_id == "hdfc_flexi_cap"
    assert resolution.field == "exit_load"


def test_two_schemes_are_ambiguous(settings: Settings) -> None:
    resolution = resolve_query(
        "Large Cap or Flexi Cap expense ratio?", settings=settings
    )

    assert resolution.needs_clarification is True
    assert resolution.source == "ambiguous"
    assert {item.scheme_id for item in resolution.ambiguous} == {
        "hdfc_large_cap",
        "hdfc_flexi_cap",
    }
    assert resolution.clarify_options == resolution.ambiguous


def test_missing_scheme_clarifies_with_four_options(settings: Settings) -> None:
    resolution = resolve_query("What is the minimum SIP?", settings=settings)

    assert resolution.needs_clarification is True
    assert resolution.field == "minimum_sip"
    assert {item.scheme_id for item in resolution.clarify_options} == {
        "hdfc_large_cap",
        "hdfc_flexi_cap",
        "hdfc_elss",
        "hdfc_mid_cap",
    }


def test_amc_wide_question_resolves_amc(settings: Settings) -> None:
    resolution = resolve_query("fund size of HDFC AMC", settings=settings)

    assert resolution.scheme_id == "amc_hdfc"
    assert resolution.field == "fund_size"


def test_statement_query_resolves_amc_and_field(settings: Settings) -> None:
    resolution = resolve_query("capital gains statement download", settings=settings)

    assert resolution.scheme_id == "amc_hdfc"
    assert resolution.field == "statement_download"


def test_multi_field_question_resolves_a_primary_field(settings: Settings) -> None:
    resolution = resolve_query(
        "expense ratio and exit load of HDFC ELSS", settings=settings
    )

    assert resolution.scheme_id == "hdfc_elss"
    # G28: a multi-field question answers the primary (longest) field.
    assert resolution.field == "expense_ratio"


def test_follow_up_uses_last_scheme(settings: Settings) -> None:
    resolution = resolve_query(
        "What about the exit load?",
        settings=settings,
        last_scheme="hdfc_mid_cap",
        turn_count=1,
    )

    assert resolution.scheme_id == "hdfc_mid_cap"
    assert resolution.field == "exit_load"
    assert resolution.follow_up is True


def test_follow_up_expires_after_three_turns(settings: Settings) -> None:
    resolution = resolve_query(
        "What about the exit load?",
        settings=settings,
        last_scheme="hdfc_mid_cap",
        turn_count=3,
    )

    assert resolution.scheme_id is None
    assert resolution.needs_clarification is True
