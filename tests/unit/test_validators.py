"""Answer validation, repair-once, and extractive fallback tests (§7.6, §9.4)."""

from __future__ import annotations

from mf_faq.guardrails.sentences import count_sentences
from mf_faq.guardrails.validators import (
    answer_supports_numbers,
    ensure_valid_answer,
    extractive_answer,
    is_plain_text,
    numbers_in,
    strip_markup,
    validate_answer,
)

EVIDENCE = ["HDFC ELSS Tax Saver Fund Direct Plan Growth > Expense ratio: 1.20%"]


def test_three_sentences_pass_and_four_fail() -> None:
    three = validate_answer("One. Two. Three.")
    four = validate_answer("One. Two. Three. Four.")

    assert three.ok is True
    assert four.ok is False
    assert "too_many_sentences" in four.issues


def test_empty_answer_fails() -> None:
    result = validate_answer("   ")

    assert result.ok is False
    assert "empty" in result.issues


def test_plain_text_is_enforced_by_stripping_markup() -> None:
    raw = "<b>HDFC ELSS</b> has a [1.20%](https://evil.example) expense ratio."
    result = validate_answer(raw)

    assert "<" not in result.answer
    assert ">" not in result.answer
    assert "evil.example" not in result.answer
    assert is_plain_text(result.answer) is True


def test_advice_language_fails_validation() -> None:
    result = validate_answer("You should buy this fund because it is the best.")

    assert result.ok is False
    assert "advice_language" in result.issues
    assert result.status_override == "refused_advice"


def test_performance_language_fails_validation() -> None:
    result = validate_answer("The 5-year return of this fund is 12%.")

    assert result.ok is False
    assert "performance_language" in result.issues
    assert result.status_override == "refused_performance"


def test_pii_echo_fails_validation_without_echoing_values() -> None:
    result = validate_answer("Your PAN ABCDE1234F is on file.")

    assert result.ok is False
    assert "pii_echo" in result.issues


def test_ungrounded_number_fails_when_checking_numbers() -> None:
    result = validate_answer(
        "The expense ratio is 0.85%.",
        evidence_texts=EVIDENCE,
        check_numbers=True,
    )

    assert result.ok is False
    assert "ungrounded_number" in result.issues


def test_grounded_number_passes() -> None:
    result = validate_answer(
        "The expense ratio is 1.20%.",
        evidence_texts=EVIDENCE,
        check_numbers=True,
    )

    assert result.ok is True


def test_number_normalisation_handles_commas_and_percentages() -> None:
    assert numbers_in("₹1,20,000 and 0.80%") == ["120000", "0.8"]
    assert answer_supports_numbers(
        "The fund size is ₹1,20,000 Cr.",
        ["Fund size: ₹1,20,000 Cr"],
    )


def test_extractive_answer_uses_chunk_value_and_three_sentences() -> None:
    text = "HDFC ELSS > Lock-in: ELSS • 3Y Lock-in"
    answer = extractive_answer(text)

    assert answer == "ELSS • 3Y Lock-in."


def test_repair_once_then_extractive_fallback() -> None:
    repair_calls: list[str] = []

    def repair(clean: str, issues: list[str]) -> str:
        repair_calls.append(clean)
        return "Still four. Sentences. Here. Fail."

    result = ensure_valid_answer(
        "One. Two. Three. Four.",
        max_sentences=3,
        evidence_texts=EVIDENCE,
        repair=repair,
        fallback_text="The expense ratio is 1.20%.",
    )

    assert len(repair_calls) == 1
    assert result.ok is True
    assert result.fallback is True
    assert result.repaired is True
    assert result.answer == "The expense ratio is 1.20%."


def test_successful_repair_avoids_fallback() -> None:
    def repair(clean: str, issues: list[str]) -> str:
        return "The expense ratio is 1.20%."

    result = ensure_valid_answer(
        "One. Two. Three. Four.",
        max_sentences=3,
        evidence_texts=EVIDENCE,
        repair=repair,
        fallback_text="Fallback sentence.",
    )

    assert result.ok is True
    assert result.repaired is True
    assert result.fallback is False
    assert result.answer == "The expense ratio is 1.20%."


def test_four_sentence_answer_falls_back_to_three_sentences() -> None:
    # G34: without a usable repair, the extractive fallback is capped at 3.
    result = ensure_valid_answer(
        "One. Two. Three. Four.",
        max_sentences=3,
        evidence_texts=EVIDENCE,
        repair=None,
        fallback_text="The expense ratio is 1.20%.",
    )

    assert result.ok is True
    assert result.fallback is True
    assert result.answer == "The expense ratio is 1.20%."


def test_extractive_answer_truncates_a_long_chunk() -> None:
    text = "HDFC ELSS > Note: One. Two. Three. Four."
    answer = extractive_answer(text)

    assert count_sentences(answer) <= 3
    assert answer == "One. Two. Three."


def test_advice_language_is_not_masked_by_extractive_fallback() -> None:
    result = ensure_valid_answer(
        "You should buy this fund.",
        max_sentences=3,
        evidence_texts=EVIDENCE,
        repair=lambda clean, issues: "The expense ratio is 1.20%.",
        fallback_text="The expense ratio is 1.20%.",
    )

    assert result.ok is False
    assert result.status_override == "refused_advice"
    assert result.fallback is False


def test_performance_language_is_not_masked_by_extractive_fallback() -> None:
    result = ensure_valid_answer(
        "The 5-year return is 12%.",
        max_sentences=3,
        evidence_texts=EVIDENCE,
        repair=lambda clean, issues: "The expense ratio is 1.20%.",
        fallback_text="The expense ratio is 1.20%.",
    )

    assert result.ok is False
    assert result.status_override == "refused_performance"
    assert result.fallback is False


def test_strip_markup_removes_urls_and_decoration() -> None:
    cleaned = strip_markup("**Bold** [link](https://x.example) https://y.example")

    assert cleaned == "Bold link"
