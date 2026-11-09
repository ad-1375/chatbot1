"""Field taxonomy: canonical synonyms, longest match, label precedence."""

from __future__ import annotations

import pytest

from mf_faq.chunking.taxonomy import (
    CANONICAL_FIELDS,
    FIELD_DISPLAY,
    field_display,
    match_field,
    resolve_field,
    synonyms_for,
)


@pytest.mark.parametrize("field", [field for field in CANONICAL_FIELDS if field != "other"])
def test_every_canonical_field_resolves_from_its_synonyms(field: str) -> None:
    synonyms = synonyms_for(field)
    assert synonyms, f"{field} has no synonyms"
    for synonym in synonyms:
        assert match_field(synonym) == field, synonym


def test_display_labels_exist_for_every_field() -> None:
    assert set(FIELD_DISPLAY) == set(CANONICAL_FIELDS)
    assert field_display("expense_ratio") == "Expense ratio"
    assert field_display("not_a_field") == "Fact"


def test_exit_does_not_override_exit_load() -> None:
    assert match_field("Exit load") == "exit_load"
    assert match_field("Exit load applicable after 1 year") == "exit_load"
    assert match_field("Exit") == "exit_load"


def test_hyphen_and_case_normalisation() -> None:
    assert match_field("LOCK-IN PERIOD") == "lock_in"
    assert match_field("Risk-o-meter") == "riskometer"
    assert match_field("Min. for SIP") == "minimum_sip"


def test_cap_types_resolve_to_category() -> None:
    assert match_field("Large Cap") == "category"
    assert match_field("Mid Cap") == "category"
    assert match_field("Flexi Cap") == "category"


def test_unknown_text_maps_to_other() -> None:
    assert match_field("Something completely unrelated") is None
    assert resolve_field(label="Something completely unrelated") == "other"
    assert resolve_field(text="No taxonomy field here") == "other"


def test_label_wins_over_section() -> None:
    # A block under the exit-load heading must not inherit exit_load when its
    # own label names a different fact.
    assert (
        resolve_field(
            label="Stamp duty on investment",
            text="Stamp duty on investment: 0.005%",
            section_path=["HDFC ELSS", "Exit load"],
        )
        == "other"
    )
    assert (
        resolve_field(
            label="Exit load",
            text="Exit load: 1% within 1 year",
            section_path=["HDFC ELSS", "Fund details"],
        )
        == "exit_load"
    )


def test_section_and_header_fallbacks() -> None:
    assert (
        resolve_field(section_path=["HDFC ELSS", "Expense ratio"], text="1.20%")
        == "expense_ratio"
    )
    assert resolve_field(header=["Field", "Exit load"]) == "exit_load"
