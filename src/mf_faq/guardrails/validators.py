"""Validate sentences, plain text, advice/performance language, and grounding.

Validation is deterministic and independent of the provider. The answer service
uses :func:`ensure_valid_answer` to apply the §7.6 order: validate, repair once,
then use the extractive fallback built from the top evidence chunk.

See ``implementation.md`` §7.6 and §9.4.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Sequence

from mf_faq.guardrails.intent import (
    has_advice_language,
    has_performance_language,
)
from mf_faq.guardrails.pii import detect_pii
from mf_faq.guardrails.sentences import count_sentences, truncate_sentences

__all__ = [
    "ValidationResult",
    "answer_supports_numbers",
    "extractive_answer",
    "ensure_valid_answer",
    "is_plain_text",
    "numbers_in",
    "strip_markup",
    "validate_answer",
]

_HTML_RE = re.compile(r"<[^>]+>")
_MARKDOWN_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_MARKDOWN_DECORATION_RE = re.compile(r"(\*\*|__|~~|`)")
_BARE_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
_WHITESPACE_RE = re.compile(r"\s+")


@dataclass
class ValidationResult:
    ok: bool
    answer: str
    issues: list[str]
    fallback: bool = False
    repaired: bool = False

    @property
    def status_override(self) -> str | None:
        if "advice_language" in self.issues:
            return "refused_advice"
        if "performance_language" in self.issues:
            return "refused_performance"
        return None


def strip_markup(text: str) -> str:
    """Return plain text: strip HTML, Markdown links, decoration, and URLs."""
    cleaned = _HTML_RE.sub(" ", text or "")
    cleaned = _MARKDOWN_IMAGE_RE.sub(r"\1", cleaned)
    cleaned = _MARKDOWN_LINK_RE.sub(r"\1", cleaned)
    cleaned = _MARKDOWN_DECORATION_RE.sub("", cleaned)
    cleaned = _BARE_URL_RE.sub("", cleaned)
    cleaned = _WHITESPACE_RE.sub(" ", cleaned)
    return cleaned.strip(" \t\r\n-")


def is_plain_text(text: str) -> bool:
    if "<" in text or ">" in text:
        return False
    if _MARKDOWN_LINK_RE.search(text) or _MARKDOWN_IMAGE_RE.search(text):
        return False
    if _MARKDOWN_DECORATION_RE.search(text):
        return False
    if _BARE_URL_RE.search(text):
        return False
    return True


def numbers_in(text: str) -> list[str]:
    """Extract numeric tokens with commas/percent signs normalised."""
    values: list[str] = []
    for raw in _NUMBER_RE.findall(text or ""):
        normalized = raw.replace(",", "").rstrip(".")
        try:
            normalized = str(float(normalized)).rstrip("0").rstrip(".")
        except ValueError:
            normalized = raw
        if normalized:
            values.append(normalized)
    return values


def answer_supports_numbers(answer: str, evidence_texts: Sequence[str]) -> bool:
    """Every number in the answer must appear in the supplied evidence."""
    evidence_numbers = set()
    for text in evidence_texts:
        evidence_numbers.update(numbers_in(text))
    return set(numbers_in(answer)).issubset(evidence_numbers)


def validate_answer(
    answer: str,
    *,
    max_sentences: int = 3,
    evidence_texts: Sequence[str] = (),
    check_numbers: bool = False,
    allow_advice: bool = False,
    allow_performance: bool = False,
) -> ValidationResult:
    """Validate one model answer and return the cleaned text."""
    cleaned = strip_markup(answer)
    issues: list[str] = []

    if not cleaned:
        issues.append("empty")
    if cleaned and not is_plain_text(cleaned):
        issues.append("not_plain_text")
    if cleaned and count_sentences(cleaned) > max_sentences:
        issues.append("too_many_sentences")
    if not allow_advice and cleaned and has_advice_language(cleaned):
        issues.append("advice_language")
    if not allow_performance and cleaned and has_performance_language(cleaned):
        issues.append("performance_language")
    if cleaned and detect_pii(cleaned):
        issues.append("pii_echo")
    if (
        cleaned
        and check_numbers
        and evidence_texts
        and not answer_supports_numbers(cleaned, evidence_texts)
    ):
        issues.append("ungrounded_number")

    return ValidationResult(ok=not issues, answer=cleaned, issues=issues)


def _chunk_body(text: str) -> str:
    body = text.strip()
    if ": " in body:
        candidate = body.rsplit(": ", 1)[1].strip()
        if candidate:
            return candidate
    return body


def extractive_answer(
    evidence: str | object,
    *,
    max_sentences: int = 3,
) -> str:
    """Build a grounded fallback from the top evidence chunk's own text."""
    text = evidence if isinstance(evidence, str) else getattr(evidence, "text", "")
    body = _chunk_body(str(text))
    body = truncate_sentences(body, max_sentences)
    if body and not body.endswith((".", "!", "?")):
        body = f"{body}."
    return body


def ensure_valid_answer(
    raw_answer: str,
    *,
    max_sentences: int = 3,
    evidence_texts: Sequence[str] = (),
    check_numbers: bool = False,
    repair: Callable[[str, list[str]], str] | None = None,
    fallback_text: str = "",
    extractive_fallback: bool = True,
) -> ValidationResult:
    """Validate, repair at most once, then fall back to extractive text."""
    first = validate_answer(
        raw_answer,
        max_sentences=max_sentences,
        evidence_texts=evidence_texts,
        check_numbers=check_numbers,
    )
    if first.ok:
        return first

    # G40: advice or performance language in a model answer must force the
    # matching refusal template. It is never repaired or replaced by a factual
    # extractive fallback, because that would hide the policy violation.
    if {"advice_language", "performance_language"} & set(first.issues):
        first.repaired = False
        return first

    if repair is not None:
        repaired_raw = repair(first.answer, list(first.issues))
        repaired = validate_answer(
            repaired_raw,
            max_sentences=max_sentences,
            evidence_texts=evidence_texts,
            check_numbers=check_numbers,
        )
        repaired.repaired = True
        if repaired.ok:
            return repaired

    if extractive_fallback and fallback_text:
        fallback = validate_answer(
            fallback_text,
            max_sentences=max_sentences,
            evidence_texts=evidence_texts,
            check_numbers=check_numbers,
        )
        fallback.fallback = True
        fallback.repaired = bool(repair is not None)
        if fallback.ok:
            return fallback
        return fallback

    first.repaired = bool(repair is not None)
    return first
