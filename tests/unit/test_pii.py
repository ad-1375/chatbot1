"""PII detection, redaction, and security-log tests (§9.1)."""

from __future__ import annotations

import json

import pytest

from mf_faq.guardrails.pii import (
    PIIScreenResult,
    contains_pii,
    detect_pii,
    log_pii_block,
    salted_hash,
    screen_pii,
)

PII_CASES = [
    ("G01", "My PAN is ABCDE1234F", "pan"),
    ("G02", "Aadhaar 1234 5678 9012", "aadhaar"),
    ("G03", "Account 123456789012", "account"),
    ("G04", "OTP is 123456", "otp"),
    ("G05", "test@example.com", "email"),
    ("G06", "Call me on 9876543210", "phone"),
    ("G12", "Card 4111 1111 1111 1111", "card"),
]

SAFE_CASES = [
    ("G07", "Minimum SIP is ₹500"),
    ("G08", "Expense ratio 0.75%"),
    ("G09", "08 Nov 2026"),
    ("G10", "HDFCELSS"),
    ("G11", "Section 80C"),
]


@pytest.mark.parametrize(("case_id", "text", "expected"), PII_CASES)
def test_every_pii_type_is_blocked(case_id: str, text: str, expected: str) -> None:
    result = screen_pii(text)

    assert result.blocked is True, case_id
    assert expected in result.pii_types, case_id


@pytest.mark.parametrize(("case_id", "text"), SAFE_CASES)
def test_amounts_percentages_dates_and_codes_pass(case_id: str, text: str) -> None:
    result = screen_pii(text)

    assert result.blocked is False, case_id
    assert result.pii_types == ()
    assert contains_pii(text) is False


def test_detect_returns_positions_for_review() -> None:
    matches = detect_pii("PAN ABCDE1234F")

    assert matches
    assert matches[0].pii_type == "pan"
    assert matches[0].end > matches[0].start


def test_blocked_result_contains_no_raw_query() -> None:
    raw = "My PAN is ABCDE1234F and OTP is 123456"
    result = screen_pii(raw)

    assert result.blocked is True
    assert raw not in repr(result)
    assert "ABCDE1234F" not in result.salted_hash
    assert "123456" not in result.salted_hash


def test_salted_hash_is_deterministic_and_salt_sensitive() -> None:
    first = salted_hash("test@example.com", salt="a")
    second = salted_hash("test@example.com", salt="a")
    third = salted_hash("test@example.com", salt="b")

    assert first == second
    assert first != third
    assert first.startswith("sha256:")


def test_security_log_is_json_without_raw_query(tmp_path) -> None:
    raw = "My PAN is ABCDE1234F"
    result = screen_pii(raw)
    log_path = tmp_path / "security" / "pii_blocks.jsonl"

    written = log_pii_block(
        result,
        trace_id="trace-1",
        session_id="session-1",
        log_path=log_path,
    )

    assert written == log_path
    line = log_path.read_text(encoding="utf-8").strip()
    payload = json.loads(line)
    assert payload["trace_id"] == "trace-1"
    assert payload["session_id"] == "session-1"
    assert payload["pii_types"] == ["pan"]
    assert payload["query_hash"].startswith("sha256:")
    assert raw not in line
    assert "ABCDE1234F" not in line


def test_log_requires_a_blocked_result(tmp_path) -> None:
    with pytest.raises(ValueError):
        log_pii_block(
            PIIScreenResult(blocked=False),
            trace_id="t",
            session_id="s",
            log_path=tmp_path / "pii.jsonl",
        )
