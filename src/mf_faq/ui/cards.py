"""Response cards for every answer status.

Every rendering path is plain text: model and page text is HTML-escaped before
it reaches ``st.markdown`` and links are rendered only after the citation was
resolved and allowlist-checked. The status is always text plus colour (an alert
box), never colour alone.

See ``implementation.md`` §8.2 and §8.4.
"""

from __future__ import annotations

import html
from typing import Any, Callable

import streamlit as st

from mf_faq.contracts import AnswerResponse, Citation
from mf_faq.generation.citation import (
    ReferenceRegistry,
    is_allowlisted_citation,
    normalize_url,
)

__all__ = ["STATUS_LABELS", "render_response"]

STATUS_LABELS: dict[str, str] = {
    "answered": "Answered",
    "clarify": "Needs clarification",
    "not_found": "Not found in the indexed sources",
    "refused_advice": "Refused: investment advice",
    "refused_performance": "Refused: performance figures",
    "out_of_scope": "Out of scope",
    "blocked_pii": "Blocked: personal information",
    "error": "Error",
}

_STATUS_COLOURS: dict[str, str] = {
    "answered": "green",
    "clarify": "orange",
    "not_found": "blue",
    "refused_advice": "orange",
    "refused_performance": "orange",
    "out_of_scope": "blue",
    "blocked_pii": "red",
    "error": "red",
}


def _alert(status: str, text: str) -> None:
    colour = _STATUS_COLOURS.get(status, "blue")
    if colour == "green":
        st.success(text)
    elif colour == "orange":
        st.warning(text)
    elif colour == "red":
        st.error(text)
    else:
        st.info(text)


def _escape(text: str) -> str:
    return html.escape(text or "", quote=False)


def _valid_citation(
    response: AnswerResponse,
    registry: ReferenceRegistry,
) -> Citation | None:
    citation = response.citation
    if citation is None:
        return None
    normalized = normalize_url(citation.url)
    if not is_allowlisted_citation(citation.url, registry):
        return None
    if response.status == "answered":
        if normalized not in registry.corpus_urls:
            return None
        evidence_urls = {
            normalize_url(item.url) for item in response.evidence if item.url
        }
        if evidence_urls and normalized not in evidence_urls:
            return None
    return citation


def _render_citation(
    response: AnswerResponse,
    registry: ReferenceRegistry,
) -> None:
    if response.status == "clarify" or response.status == "blocked_pii":
        return
    citation = _valid_citation(response, registry)
    if citation is None:
        if response.status in {
            "answered",
            "not_found",
            "refused_advice",
            "refused_performance",
            "error",
        }:
            st.caption("Citation unavailable.")
        return
    st.link_button(f"Source: {citation.title}", citation.url)
    if citation.section:
        st.caption(f"Section: {citation.section}")


def _render_evidence(response: AnswerResponse) -> None:
    if not response.evidence:
        return
    with st.expander(f"Evidence ({len(response.evidence)} chunks)", expanded=False):
        for item in response.evidence:
            st.text(
                f"{item.chunk_id} · score {item.score:.3f} · "
                f"{item.section or 'n/a'} · {item.field}"
            )
            snippet = item.text if len(item.text) <= 320 else f"{item.text[:317]}..."
            st.text(snippet)


def _render_clarify_buttons(
    response: AnswerResponse,
    *,
    original_query: str,
    on_clarify: Callable[[str, str], None] | None,
) -> None:
    if on_clarify is None:
        return
    options: list[dict[str, Any]] = list(response.guardrails.get("options") or [])
    options = [option for option in options if option.get("title")]
    if not options:
        return
    columns = st.columns(min(4, len(options)))
    for index, option in enumerate(options[:4]):
        title = str(option["title"])
        scheme_id = str(option.get("scheme_id") or index)
        key = f"clarify_{response.trace_id}_{scheme_id}"
        if columns[index].button(title, key=key):
            on_clarify(original_query, title)


def render_response(
    response: AnswerResponse,
    *,
    registry: ReferenceRegistry,
    original_query: str = "",
    on_clarify: Callable[[str, str], None] | None = None,
) -> None:
    """Render one response card inside an existing ``st.chat_message`` block."""
    label = STATUS_LABELS.get(response.status, response.status)
    with st.container(border=True):
        _alert(response.status, f"**{label}**")
        if response.answer:
            # Escape first; ``unsafe_allow_html`` stays False.
            st.markdown(_escape(response.answer), unsafe_allow_html=False)

        _render_citation(response, registry)

        if response.last_updated:
            st.caption(f"Last updated from sources: {response.last_updated}")

        if response.status == "clarify":
            _render_clarify_buttons(
                response,
                original_query=original_query,
                on_clarify=on_clarify,
            )

        _render_evidence(response)

        st.caption(
            f"Trace {response.trace_id[:8]} · Open the Pipeline tab to inspect "
            "retrieval and answer traces."
        )
