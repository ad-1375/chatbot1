"""Advice, performance, and out-of-scope intent tests (§9.2)."""

from __future__ import annotations

import pytest

from mf_faq.guardrails.intent import (
    has_advice_language,
    has_performance_language,
    screen_intent,
)

INTENT_CASES = [
    ("G13", "Should I buy HDFC ELSS?", "refused_advice"),
    ("G14", "Which HDFC fund is best?", "refused_advice"),
    ("G15", "Is this fund suitable for my portfolio?", "refused_advice"),
    ("G16", "What is the 5-year return of HDFC Mid Cap?", "refused_performance"),
    ("G17", "Compare Large Cap and Flexi Cap returns", "refused_performance"),
    ("G18", "What is the CAGR?", "refused_performance"),
    ("G19", "Tell me about SBI Bluechip Fund", "out_of_scope"),
    ("G20", "What is HDFC Balanced Advantage NAV?", "out_of_scope"),
    ("G21", "File my income tax return", "out_of_scope"),
]


@pytest.mark.parametrize(("case_id", "text", "status"), INTENT_CASES)
def test_refusal_intents(case_id: str, text: str, status: str) -> None:
    decision = screen_intent(text)

    assert decision.status == status, case_id


def test_factual_questions_default_to_factual() -> None:
    for text in (
        "What is the expense ratio of HDFC ELSS Tax Saver?",
        "Exit load on HDFC Flexi Cap",
        "What is the exit load on HDFC Flexi Cap Fund?",
        "What is the exit load on HDFC ELSS Tax Saver Fund?",
        "What is the exit load on HDFC Mid Cap Fund?",
        "Minimum SIP for HDFC Mid Cap",
        "ELSS lock-in",
        "Riskometer of HDFC Large Cap",
        "Benchmark of HDFC Flexi Cap",
        "Capital-gains statement download",
    ):
        decision = screen_intent(text)
        assert decision.intent == "factual", text
        assert decision.status is None, text


def test_exit_as_an_action_is_still_advice() -> None:
    for text in (
        "Should I exit HDFC ELSS Tax Saver Fund?",
        "When should I exit the fund?",
        "Is it time to exit this scheme?",
    ):
        assert screen_intent(text).status == "refused_advice", text


def test_empty_query_is_factual() -> None:
    assert screen_intent("   ").intent == "factual"


def test_model_answer_language_helpers() -> None:
    assert has_advice_language("You should buy this fund.") is True
    assert has_advice_language("The exit load is nil.") is False
    assert has_performance_language("The 5-year return is 12%.") is True
    assert has_performance_language("The benchmark is Nifty 500.") is False
