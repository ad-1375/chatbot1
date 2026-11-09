"""Sentence splitting, counting, and truncation for answers.

The splitter is deliberately conservative: it never breaks on common
abbreviations, month names, single-letter initials, or a decimal point. This
keeps ``count_sentences`` aligned with what a reader perceives as a sentence.

See ``implementation.md`` §9.4 (four-sentence answers) and §7.6.
"""

from __future__ import annotations

import re

__all__ = [
    "ABBREVIATIONS",
    "count_sentences",
    "split_sentences",
    "truncate_sentences",
]

# Abbreviations whose trailing period is not a sentence boundary.
ABBREVIATIONS: frozenset[str] = frozenset(
    {
        "mr",
        "mrs",
        "ms",
        "dr",
        "prof",
        "rs",
        "ltd",
        "inc",
        "co",
        "corp",
        "vs",
        "etc",
        "e.g",
        "i.e",
        "no",
        "st",
        "approx",
        "jan",
        "feb",
        "mar",
        "apr",
        "jun",
        "jul",
        "aug",
        "sep",
        "sept",
        "oct",
        "nov",
        "dec",
        "u.s",
        "u.k",
        "a.m",
        "p.m",
    }
)

_BOUNDARY_RE = re.compile(r"(?<=[.!?])\s+")
_ABBREVIATION_TAIL_RE = re.compile(r"(?:^|\s)([^\s]+)\.$")


def _ends_with_abbreviation(text: str) -> bool:
    stripped = text.rstrip()
    if not stripped.endswith("."):
        return False
    match = _ABBREVIATION_TAIL_RE.search(stripped)
    if not match:
        return False
    word = match.group(1).lower()
    if word in ABBREVIATIONS:
        return True
    # A single initial such as "V." or "A." does not end a sentence.
    return len(word) == 1 and word.isalpha()


def _starts_lowercase(text: str) -> bool:
    return bool(text) and text[0].islower()


def split_sentences(text: str) -> list[str]:
    """Split prose at real sentence boundaries."""
    if not text or not text.strip():
        return []

    parts = [part.strip() for part in _BOUNDARY_RE.split(text.strip()) if part.strip()]
    merged: list[str] = []
    for part in parts:
        if merged and (
            _ends_with_abbreviation(merged[-1]) or _starts_lowercase(part)
        ):
            merged[-1] = f"{merged[-1]} {part}"
        else:
            merged.append(part)
    return merged


def count_sentences(text: str) -> int:
    """Return the number of sentences in ``text``."""
    return len(split_sentences(text))


def truncate_sentences(text: str, max_sentences: int) -> str:
    """Return at most ``max_sentences`` sentences, preserving punctuation."""
    if max_sentences <= 0:
        return ""
    return " ".join(split_sentences(text)[:max_sentences]).strip()
