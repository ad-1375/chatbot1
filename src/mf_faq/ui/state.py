"""Session state, example questions, and query submission for the UI.

The UI keeps the conversation in ``st.session_state`` only. Nothing is written
server-side: the answer service still writes its normal, redacted traces, but
there is no chat-store. PII-blocked queries are never copied into a chat bubble
or retained in session state.

See ``implementation.md`` §8.2.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import streamlit as st

from mf_faq.config import Settings
from mf_faq.contracts import AnswerResponse, QueryRequest
from mf_faq.guardrails.pii import screen_pii
from mf_faq.io_utils import read_json
from mf_faq.retrieval.resolver import SCHEME_ALIASES, SCHEME_TITLES, Resolution, resolve_query

__all__ = [
    "BLOCKED_QUERY_PLACEHOLDER",
    "EXAMPLES",
    "WELCOME",
    "clear_conversation",
    "init_session_state",
    "last_refreshed",
    "provider_summary",
    "set_pending_submission",
    "sources_indexed",
    "submit_query",
    "take_pending_submission",
]

EXAMPLES: tuple[str, ...] = (
    "What is the expense ratio of HDFC ELSS Tax Saver Fund?",
    "What is the exit load on HDFC Flexi Cap Fund?",
    "What is the minimum SIP for HDFC Mid Cap Fund?",
)

WELCOME = (
    "Ask a short factual question about the HDFC AMC overview or the HDFC Large Cap, "
    "Flexi Cap, ELSS Tax Saver, and Mid Cap funds. Answers come only from the five "
    "indexed Groww pages, with one source link and a freshness date."
)

BLOCKED_QUERY_PLACEHOLDER = "Message blocked: personal information"

ERROR_COPY = (
    "I couldn't complete that request. Please try again or ask a simpler factual "
    "question."
)

# Only scheme-identifying aliases are stripped when a clarify button is clicked.
# Field cues such as "capital gains statement" stay in the query.
_SCHEME_HINTS: tuple[str, ...] = tuple(
    sorted(
        {
            alias
            for scheme_id, aliases in SCHEME_ALIASES.items()
            if scheme_id != "amc_hdfc"
            for alias in aliases
        }
        | {
            title
            for scheme_id, title in SCHEME_TITLES.items()
            if scheme_id != "amc_hdfc"
        },
        key=len,
        reverse=True,
    )
)

_MEMORY_TURNS = 3
_WHITESPACE_RE = re.compile(r"\s+")


def init_session_state(st_module: Any, settings: Settings) -> None:
    """Create conversation keys once; never overwrite an injected test service."""
    defaults: dict[str, Any] = {
        "session_id": uuid.uuid4().hex,
        "messages": [],
        "last_scheme": None,
        "turn_count": 0,
        "last_trace_id": None,
        "_pending_submission": None,
    }
    for key, value in defaults.items():
        if key not in st_module.session_state:
            st_module.session_state[key] = value


def set_pending_submission(
    st_module: Any,
    query: str,
    *,
    scheme_hint: str | None = None,
) -> None:
    """Queue a submission so example buttons and clarify buttons can rerun safely."""
    st_module.session_state["_pending_submission"] = {
        "query": query,
        "scheme_hint": scheme_hint,
    }


def take_pending_submission(st_module: Any) -> dict[str, Any] | None:
    return st_module.session_state.pop("_pending_submission", None)


def clear_conversation(st_module: Any) -> None:
    """Reset conversation state only; artifacts and the answer service stay warm."""
    st_module.session_state["messages"] = []
    st_module.session_state["last_scheme"] = None
    st_module.session_state["turn_count"] = 0
    st_module.session_state["last_trace_id"] = None
    st_module.session_state["_pending_submission"] = None
    st_module.session_state["session_id"] = uuid.uuid4().hex


def _strip_scheme_mentions(text: str) -> str:
    """Remove named schemes so a clarify choice cannot create a new ambiguity."""
    cleaned = text or ""
    for alias in _SCHEME_HINTS:
        pattern = rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])"
        cleaned = re.sub(pattern, " ", cleaned, flags=re.IGNORECASE)
    return _WHITESPACE_RE.sub(" ", cleaned).strip(" ?.,")


def apply_scheme_hint(query: str, scheme_title: str) -> str:
    """Re-submit the original question with exactly one named scheme."""
    base = _strip_scheme_mentions(query)
    if base:
        return f"{base} {scheme_title}"
    return scheme_title


def _update_memory(
    st_module: Any,
    resolution: Resolution | None,
    status: str,
) -> None:
    if resolution is None or resolution.scheme_id is None:
        return
    if status in {
        "blocked_pii",
        "refused_advice",
        "refused_performance",
        "out_of_scope",
        "error",
    }:
        return

    if resolution.follow_up:
        next_count = int(st_module.session_state.get("turn_count", 0)) + 1
        if next_count >= _MEMORY_TURNS:
            st_module.session_state["last_scheme"] = None
            st_module.session_state["turn_count"] = 0
        else:
            st_module.session_state["turn_count"] = next_count
        return

    st_module.session_state["last_scheme"] = resolution.scheme_id
    st_module.session_state["turn_count"] = 0


def _error_response() -> AnswerResponse:
    return AnswerResponse(
        trace_id=uuid.uuid4().hex,
        status="error",
        answer=ERROR_COPY,
        citation=None,
    )


def submit_query(
    st_module: Any,
    service: Any,
    settings: Settings,
    query: str,
    *,
    scheme_hint: str | None = None,
) -> AnswerResponse | None:
    """Resolve, submit, remember the scheme, and append display-safe messages."""
    raw_query = (query or "").strip()
    if not raw_query:
        return None
    if scheme_hint:
        raw_query = apply_scheme_hint(raw_query, scheme_hint)

    # Screen before resolving so a PII query never enters the resolver or the
    # transcript. The service screens again as the authoritative guard.
    pii = screen_pii(raw_query)
    resolution: Resolution | None = None
    if not pii.blocked:
        try:
            resolution = resolve_query(
                raw_query,
                settings=settings,
                last_scheme=st_module.session_state.get("last_scheme"),
                turn_count=int(st_module.session_state.get("turn_count", 0)),
            )
        except Exception:
            resolution = None

    request = QueryRequest(
        query=raw_query,
        session_id=str(st_module.session_state.get("session_id") or uuid.uuid4().hex),
        last_scheme=st_module.session_state.get("last_scheme"),
        turn_count=max(0, int(st_module.session_state.get("turn_count", 0))),
    )

    try:
        response = service.answer(request)
    except Exception:
        # The service is fail-safe; this is a final UI-only guard.
        response = _error_response()

    blocked = response.status == "blocked_pii"
    user_text = BLOCKED_QUERY_PLACEHOLDER if blocked else raw_query
    st_module.session_state["messages"].append({"role": "user", "text": user_text})
    st_module.session_state["messages"].append(
        {
            "role": "assistant",
            "response": response,
            # Never retain a blocked query in session state.
            "query": "" if blocked else raw_query,
        }
    )
    st_module.session_state["last_trace_id"] = response.trace_id
    _update_memory(st_module, resolution, response.status)
    return response


def _timestamp(value: Any) -> str | None:
    if not value:
        return None
    text = str(value)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return text[:10] if len(text) >= 10 else None


def last_refreshed(settings: Settings) -> str:
    """Return the newest artifact date, which is the corpus refresh date."""
    candidates: list[str] = []
    for relative in (
        "data/manifests/load_manifest.json",
        "data/embeddings/embedding_manifest.json",
        "data/index_manifest.json",
    ):
        path: Path = settings.resolved_path(relative)
        if not path.is_file():
            continue
        try:
            payload = read_json(path)
        except (OSError, ValueError):
            continue
        if isinstance(payload, dict):
            date = _timestamp(payload.get("created_at") or payload.get("generated_at"))
            if date:
                candidates.append(date)
    return max(candidates) if candidates else "unknown"


def sources_indexed(settings: Settings) -> int:
    return len(settings.corpus.seed_pages)


def provider_summary(settings: Settings) -> str:
    """Show provider and model names but only ``present``/``missing`` for the key."""
    provider = settings.llm.provider or "not configured"
    model = settings.llm.model or "not set"
    key_state = "present" if settings.provider_key_present() else "missing"
    return f"LLM provider: {provider} · model: {model} · key: {key_state}"
