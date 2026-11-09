"""Sentence splitter, counter, and truncation tests."""

from __future__ import annotations

from mf_faq.guardrails.sentences import count_sentences, split_sentences, truncate_sentences


def test_one_to_three_sentences_pass_counting() -> None:
    assert count_sentences("One sentence.") == 1
    assert count_sentences("One. Two.") == 2
    assert count_sentences("One. Two. Three.") == 3


def test_four_sentences_are_counted_as_four() -> None:
    assert count_sentences("One. Two. Three. Four.") == 4


def test_decimal_and_percentage_do_not_split() -> None:
    text = "The expense ratio is 0.80% and the exit load is 1.25%."
    assert split_sentences(text) == [text]


def test_dates_and_abbreviations_do_not_create_false_breaks() -> None:
    text = "Dr. Rao confirmed the 08 Nov 2026 date. It is final."
    assert split_sentences(text) == [
        "Dr. Rao confirmed the 08 Nov 2026 date.",
        "It is final.",
    ]


def test_single_initial_does_not_split() -> None:
    text = "The manager is A. Kumar. He manages the fund."
    assert count_sentences(text) == 2


def test_truncate_sentences_keeps_punctuation() -> None:
    text = "One. Two. Three. Four."
    assert truncate_sentences(text, 3) == "One. Two. Three."
    assert truncate_sentences(text, 0) == ""
    assert truncate_sentences(text, 10) == text
