"""Detect PAN, Aadhaar, account numbers, OTPs, emails, phones, and cards.

The screen is intentionally deterministic and pattern-based. A hit returns
``blocked_pii`` with the matched *types* and a salted hash; the raw query is
never stored in the result, a trace, or a log line.

See ``implementation.md`` §9.1.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

__all__ = [
    "PIIMatch",
    "PIIScreenResult",
    "contains_pii",
    "detect_pii",
    "log_pii_block",
    "salted_hash",
    "screen_pii",
]

DEFAULT_SALT = "mf-faq-pii-v1"
HASH_ENV = "PII_HASH_SALT"

PAN_RE = re.compile(
    r"(?<![A-Za-z0-9])[A-Za-z]{5}[0-9]{4}[A-Za-z](?![A-Za-z0-9])"
)
AADHAAR_KEYWORD_RE = re.compile(r"\b(?:aadhaar|aadhar)\b", re.IGNORECASE)
AADHAAR_GROUPED_RE = re.compile(r"(?<!\d)\d{4}[\s-]\d{4}[\s-]\d{4}(?!\d)")
AADHAAR_CONTIGUOUS_RE = re.compile(r"(?<!\d)\d{12}(?!\d)")
ACCOUNT_KEYWORD_RE = re.compile(r"\b(?:account|accounts|acct|a/c)\b", re.IGNORECASE)
ACCOUNT_NUMBER_RE = re.compile(r"(?<!\d)\d{9,18}(?!\d)")
OTP_KEYWORD_RE = re.compile(
    r"\b(?:otp|o\.t\.p|one[\s-]?time[\s-]?password)\b", re.IGNORECASE
)
OTP_NUMBER_RE = re.compile(r"(?<!\d)\d{4,8}(?!\d)")
EMAIL_RE = re.compile(
    r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![\w.-])"
)
PHONE_RE = re.compile(
    r"(?<![\d])(?:\+?91[\s-]?)?[6-9]\d{9}(?![\d])"
)
CARD_KEYWORD_RE = re.compile(r"\b(?:card|credit card|debit card)\b", re.IGNORECASE)
CARD_RE = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")

# Fixed order keeps the reported types deterministic.
_PATTERN_ORDER: tuple[str, ...] = (
    "pan",
    "aadhaar",
    "account",
    "otp",
    "email",
    "phone",
    "card",
)

_PROXIMITY_WINDOW = 48


@dataclass(frozen=True)
class PIIMatch:
    pii_type: str
    start: int
    end: int


@dataclass(frozen=True)
class PIIScreenResult:
    blocked: bool
    pii_types: tuple[str, ...] = ()
    salted_hash: str = ""
    matches: tuple[PIIMatch, ...] = field(default=(), repr=False)

    @property
    def types(self) -> list[str]:
        return list(self.pii_types)


def _salt(salt: str | None = None) -> str:
    return salt or os.environ.get(HASH_ENV) or DEFAULT_SALT


def salted_hash(text: str, *, salt: str | None = None) -> str:
    """Return a deterministic salted hash; never reversible in practice."""
    normalized = " ".join((text or "").split()).strip().lower()
    payload = f"{_salt(salt)}|{normalized}".encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _near_keyword(text: str, start: int, end: int, keyword: re.Pattern[str]) -> bool:
    left = max(0, start - _PROXIMITY_WINDOW)
    right = min(len(text), end + _PROXIMITY_WINDOW)
    return keyword.search(text[left:right]) is not None


def _add(matches: list[PIIMatch], pii_type: str, start: int, end: int) -> None:
    matches.append(PIIMatch(pii_type=pii_type, start=start, end=end))


def detect_pii(text: str) -> list[PIIMatch]:
    """Return every PII match in ``text`` (possibly several types)."""
    if not text:
        return []
    matches: list[PIIMatch] = []

    for match in PAN_RE.finditer(text):
        _add(matches, "pan", match.start(), match.end())

    aadhaar_keyword = AADHAAR_KEYWORD_RE.search(text)
    if aadhaar_keyword:
        _add(matches, "aadhaar", aadhaar_keyword.start(), aadhaar_keyword.end())
    for match in AADHAAR_GROUPED_RE.finditer(text):
        _add(matches, "aadhaar", match.start(), match.end())
    for match in AADHAAR_CONTIGUOUS_RE.finditer(text):
        _add(matches, "aadhaar", match.start(), match.end())

    for match in ACCOUNT_NUMBER_RE.finditer(text):
        if _near_keyword(text, match.start(), match.end(), ACCOUNT_KEYWORD_RE):
            _add(matches, "account", match.start(), match.end())

    for match in OTP_NUMBER_RE.finditer(text):
        if _near_keyword(text, match.start(), match.end(), OTP_KEYWORD_RE):
            _add(matches, "otp", match.start(), match.end())

    for match in EMAIL_RE.finditer(text):
        _add(matches, "email", match.start(), match.end())

    for match in PHONE_RE.finditer(text):
        _add(matches, "phone", match.start(), match.end())

    for match in CARD_RE.finditer(text):
        if _near_keyword(text, match.start(), match.end(), CARD_KEYWORD_RE):
            _add(matches, "card", match.start(), match.end())

    matches.sort(key=lambda item: (item.start, _PATTERN_ORDER.index(item.pii_type)))
    return matches


def _ordered_types(matches: Iterable[PIIMatch]) -> tuple[str, ...]:
    found = {match.pii_type for match in matches}
    return tuple(name for name in _PATTERN_ORDER if name in found)


def screen_pii(text: str, *, salt: str | None = None) -> PIIScreenResult:
    """Screen ``text`` and return a result that never contains the raw text."""
    matches = detect_pii(text)
    if not matches:
        return PIIScreenResult(blocked=False)
    return PIIScreenResult(
        blocked=True,
        pii_types=_ordered_types(matches),
        salted_hash=salted_hash(text, salt=salt),
        matches=tuple(matches),
    )


def contains_pii(text: str) -> bool:
    return bool(detect_pii(text))


def log_pii_block(
    result: PIIScreenResult,
    *,
    trace_id: str,
    session_id: str,
    log_path: str | Path,
    clock=None,
) -> Path:
    """Append a redacted security log line; the raw query is never accepted."""
    if not result.blocked:
        raise ValueError("log_pii_block requires a blocked PII result")
    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = (clock or (lambda: datetime.now(timezone.utc)))().isoformat()
    payload = {
        "timestamp": timestamp,
        "trace_id": trace_id,
        "session_id": session_id,
        "pii_types": list(result.pii_types),
        "query_hash": result.salted_hash,
    }
    line = f"{json.dumps(payload, ensure_ascii=False)}\n"
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())
    return path
