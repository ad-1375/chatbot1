"""Advice, performance, and out-of-scope detection.

The intent screen runs before retrieval. It is conservative on facts: a query
defaults to ``factual`` unless it clearly asks for advice, performance, or an
out-of-scope topic.

See ``implementation.md`` §9.2.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "IntentDecision",
    "has_advice_language",
    "has_performance_language",
    "screen_intent",
]

_ADVICE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "should_i",
        re.compile(
            r"\bshould\s+(?:i|we|you)\b|\bwhat\s+should\s+(?:i|we|you)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "buy_sell_hold",
        re.compile(
            r"\b(?:buy|buying|sell|selling|hold|holding|redeem|switch"
            r"|exit(?!\s+loads?\b))\b"
            r".{0,40}\b(?:fund|scheme|elss|units?|investment|portfolio)\b"
            r"|\b(?:fund|scheme|elss|units?|investment|portfolio)\b"
            r".{0,40}\b(?:buy|buying|sell|selling|hold|holding|redeem|switch"
            r"|exit(?!\s+loads?\b))\b",
            re.IGNORECASE,
        ),
    ),
    (
        "best",
        re.compile(
            r"\b(?:best|better|top|recommend(?:ed|ation)?|suitable|right\s+for\s+me|"
            r"good\s+for\s+me|worth\s+investing|good\s+time)\b"
            r".{0,40}\b(?:fund|scheme|option|investment|portfolio|it)\b"
            r"|\b(?:which|what)\b.{0,30}\b(?:fund|scheme)\b.{0,20}\b(?:best|better|top)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "portfolio_advice",
        re.compile(
            r"\b(?:my|our)\s+portfolio\b|\bportfolio\s+(?:advice|suitability|allocation)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "advice_words",
        re.compile(
            r"\b(?:advice|advise|advisory|suggest(?:ion)?|recommendation)\b",
            re.IGNORECASE,
        ),
    ),
)

_PERFORMANCE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("cagr", re.compile(r"\bcagr\b", re.IGNORECASE)),
    ("xirr", re.compile(r"\bxirr\b|\birr\b", re.IGNORECASE)),
    (
        "returns",
        re.compile(
            r"\breturns?\b|\bannuali[sz]ed\b|\btrailing\b|\babsolute\s+return",
            re.IGNORECASE,
        ),
    ),
    (
        "nav_performance",
        re.compile(
            r"\bnav\b|\bperformance\b|\boutperform\w*\b|\bunderperform\w*\b",
            re.IGNORECASE,
        ),
    ),
    (
        "risk_metrics",
        re.compile(
            r"\balpha\b|\bbeta\b|\bsharpe\b|\bstandard\s+deviation\b|\bvolatility\b",
            re.IGNORECASE,
        ),
    ),
    (
        "comparison",
        re.compile(
            r"\bcompare\b|\bcomparison\b|\bbetter\s+than\b|\bhighest\b"
            r"|\bwhich\b.{0,30}\b(?:higher|lower|best|worst)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "period_return",
        re.compile(
            r"\b(?:1|3|5|7|10)[-\s]?(?:year|yr|y)\b.{0,30}\b(?:return|growth|gain)",
            re.IGNORECASE,
        ),
    ),
)

_OTHER_AMCS: tuple[str, ...] = (
    "sbi",
    "icici",
    "axis",
    "kotak",
    "nippon",
    "mirae",
    "dsp",
    "franklin",
    "uti",
    "aditya birla",
    "tata",
    "invesco",
    "motilal",
    "quant",
    "pgim",
    "sundaram",
    "canara",
    "hsbc",
    "bandhan",
    "idfc",
    "baroda",
    "union mutual",
    "bank of india",
    "jm financial",
    "mahindra manulife",
    "ppfas",
    "parag parikh",
    "navi",
    "whiteoak",
    "iifl",
    "bajaj",
    "edelweiss",
    "shriram",
)

_OTHER_HDFC_SCHEMES: tuple[str, ...] = (
    "balanced advantage",
    "small cap",
    "multi cap",
    "contra",
    "hybrid",
    "debt",
    "gold",
    "index fund",
    "nifty 50",
    "nifty next 50",
    "banking",
    "pharma",
    "infrastructure",
    "manufacturing",
    "business cycle",
    "focused",
    "dividend yield",
    "value fund",
    "momentum",
    "children",
    "retirement",
    "arbitrage",
    "liquid",
    "overnight",
    "ultra short",
    "corporate bond",
    "credit risk",
    "gilt",
    "money market",
    "dynamic asset allocation",
    "equity savings",
    "consumption",
    "services fund",
    "mnc",
    "psu",
    "special situations",
    "housing",
    "defence",
)

_OFF_TOPIC_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(?:file|filing|prepare|e-?file)\b.{0,30}\b(?:income\s+tax|tax\s+return|itr)\b"
        r"|\bincome\s+tax\s+return\b|\bitr\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:stock|stocks|share\s+market|stock\s+market|crypto|bitcoin|"
        r"fixed\s+deposit|insurance|personal\s+loan|home\s+loan|credit\s+card|demat)\b",
        re.IGNORECASE,
    ),
)


def _find(patterns, text: str) -> list[str]:
    return [name for name, pattern in patterns if pattern.search(text)]


def _brand_pattern(name: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![a-z0-9]){re.escape(name)}(?![a-z0-9])", re.IGNORECASE)


_OTHER_AMC_PATTERNS = tuple((name, _brand_pattern(name)) for name in _OTHER_AMCS)
_OTHER_SCHEME_PATTERNS = tuple(
    (name, _brand_pattern(name)) for name in _OTHER_HDFC_SCHEMES
)


@dataclass(frozen=True)
class IntentDecision:
    intent: str
    status: str | None
    matched: tuple[str, ...] = ()

    @property
    def is_refusal(self) -> bool:
        return self.status is not None


def _out_of_scope_matches(text: str) -> list[str]:
    matched: list[str] = []
    for name, pattern in (*_OTHER_AMC_PATTERNS, *_OTHER_SCHEME_PATTERNS):
        if pattern.search(text):
            matched.append(name)
    for index, pattern in enumerate(_OFF_TOPIC_PATTERNS):
        if pattern.search(text):
            matched.append(f"off_topic_{index}")
    return matched


def screen_intent(text: str) -> IntentDecision:
    """Classify a query before any retrieval or LLM call."""
    if not text or not text.strip():
        return IntentDecision(intent="factual", status=None)

    out_of_scope = _out_of_scope_matches(text)
    if out_of_scope:
        return IntentDecision(
            intent="out_of_scope",
            status="out_of_scope",
            matched=tuple(out_of_scope),
        )

    advice = _find(_ADVICE_PATTERNS, text)
    if advice:
        return IntentDecision(
            intent="advice",
            status="refused_advice",
            matched=tuple(advice),
        )

    performance = _find(_PERFORMANCE_PATTERNS, text)
    if performance:
        return IntentDecision(
            intent="performance",
            status="refused_performance",
            matched=tuple(performance),
        )

    return IntentDecision(intent="factual", status=None)


def has_advice_language(text: str) -> bool:
    """Detect advice language in a model answer (not a user query)."""
    if not text:
        return False
    return bool(_find(_ADVICE_PATTERNS, text)) or bool(
        re.search(
            r"\b(?:you\s+should|we\s+recommend|i\s+recommend|invest\s+in|"
            r"buy|sell|hold)\b",
            text,
            re.IGNORECASE,
        )
    )


_BENCHMARK_RETURN_RE = re.compile(r"\b(?:(?:total|absolute)\s+)?return\s+index\b", re.IGNORECASE)


def has_performance_language(text: str) -> bool:
    """Detect return/performance language in a model answer.

    Benchmark names such as "NIFTY 500 Total Return Index" are index names,
    not performance claims, so they are masked before the pattern check.
    """
    if not text:
        return False
    scrubbed = _BENCHMARK_RETURN_RE.sub("benchmark index", text)
    return bool(_find(_PERFORMANCE_PATTERNS, scrubbed))
