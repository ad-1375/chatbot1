"""Streamlit entry point: Ask, Pipeline, Sources, Evaluation.

Run with::

    streamlit run src/mf_faq/ui/app.py

The app binds to localhost by default. Writes are limited to the normal answer
traces produced by the service; the UI itself never writes artifacts and never
persists chat history.

See ``implementation.md`` §8.
"""

from __future__ import annotations

import html

import streamlit as st

from mf_faq.config import Settings
from mf_faq.generation.answer import AnswerService
from mf_faq.generation.citation import load_registry
from mf_faq.ui import cards, inspector, state

DISCLAIMER = (
    "Facts-only. No investment advice. Mutual fund investments are subject to "
    "market risks; read all scheme-related documents carefully. For advice about "
    "your own situation, consult a SEBI-registered investment adviser. Do not "
    "enter PAN, Aadhaar, account numbers, OTPs, email addresses, or phone numbers."
)

_CHAT_PLACEHOLDER = "Ask a factual question about the five indexed HDFC pages"


def _settings() -> Settings:
    injected = st.session_state.get("settings")
    if injected is not None:
        return injected
    loaded = Settings.load(require_llm=False)
    st.session_state["settings"] = loaded
    return loaded


def _service(settings: Settings) -> AnswerService:
    injected = st.session_state.get("answer_service")
    if injected is not None:
        return injected
    created = AnswerService(settings)
    st.session_state["answer_service"] = created
    return created


def _submit_pending(
    settings: Settings,
    service: AnswerService,
) -> None:
    pending = state.take_pending_submission(st)
    if not pending:
        return
    state.submit_query(
        st,
        service,
        settings,
        str(pending.get("query") or ""),
        scheme_hint=pending.get("scheme_hint"),
    )


def _render_ask(
    settings: Settings,
    service: AnswerService,
    registry,
) -> None:
    st.title("HDFC MF Facts Assistant")
    st.markdown("**Facts-only. No investment advice.**")
    st.write(state.WELCOME)
    st.caption(
        f"Sources indexed: {state.sources_indexed(settings)} pages · "
        f"last refreshed {state.last_refreshed(settings)}"
    )
    st.caption(state.provider_summary(settings))
    if not settings.llm.provider or not settings.provider_key_present():
        st.info(
            "No LLM provider key is available. Advice and PII guardrails still "
            "work, but factual answers that need generation will return an error "
            "until `.env` is configured."
        )

    columns = st.columns(len(state.EXAMPLES))
    for index, example in enumerate(state.EXAMPLES):
        if columns[index].button(
            example, key=f"example_{index}", width="stretch"
        ):
            state.set_pending_submission(st, example)
            st.rerun()

    if st.button("Clear conversation", key="clear_conversation"):
        state.clear_conversation(st)
        st.rerun()

    def on_clarify(original_query: str, scheme_title: str) -> None:
        state.set_pending_submission(
            st, original_query, scheme_hint=scheme_title
        )
        st.rerun()

    for message in st.session_state.get("messages", []):
        if message.get("role") == "user":
            with st.chat_message("user"):
                st.markdown(
                    html.escape(str(message.get("text", "")), quote=False),
                    unsafe_allow_html=False,
                )
        else:
            with st.chat_message("assistant"):
                cards.render_response(
                    message["response"],
                    registry=registry,
                    original_query=str(message.get("query") or ""),
                    on_clarify=on_clarify,
                )

    prompt = st.chat_input(_CHAT_PLACEHOLDER, key="ask_chat_input")
    if prompt:
        state.set_pending_submission(st, prompt)
        st.rerun()

    st.divider()
    st.caption(DISCLAIMER)


def _render_pipeline(settings: Settings) -> None:
    st.subheader("Pipeline")
    st.caption(
        "Read-only artifacts from ingestion, chunking, embedding, indexing, "
        "retrieval, and answering."
    )
    inspector.render(
        st,
        settings,
        store=st.session_state.get("chroma_store"),
        latest_trace_id=st.session_state.get("last_trace_id"),
    )


def _render_sources(registry) -> None:
    st.subheader(f"Indexed corpus ({len(registry.corpus)} pages)")
    st.caption("These are the only pages ever fetched, chunked, or indexed.")
    for link in registry.corpus:
        st.link_button(link.title, link.url, width="stretch")

    st.subheader("Approved reference links")
    st.caption(
        "Used only in refusal and education copy. They are never fetched, "
        "chunked, or indexed."
    )
    for link in registry.references:
        st.link_button(link.title, link.url, width="stretch")


def _render_evaluation(settings: Settings) -> None:
    st.subheader("Evaluation")
    path = settings.resolved_path("docs/sample_qa.md")
    if not path.is_file():
        st.warning(f"artifact not found: {path}")
        return
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        st.error(f"artifact unreadable: {path}")
        st.caption(str(exc))
        return

    # Show only the summary section. Recorded answers, raw traces, and any
    # pasted sample values stay in the file and are never pushed to the browser.
    summary = text.split("## Recorded responses", 1)[0].strip()
    if not summary:
        st.info(
            "The acceptance set has not been recorded yet. Run the ten questions "
            "with a configured provider and save the results in "
            "`docs/sample_qa.md`."
        )
        st.caption(f"Evaluation file: {path}")
        return
    st.caption(f"Manual acceptance set · {path}")
    st.markdown(summary, unsafe_allow_html=False)


def main() -> None:
    st.set_page_config(
        page_title="HDFC MF Facts Assistant",
        page_icon="📄",
        layout="wide",
        initial_sidebar_state="collapsed",
    )
    settings = _settings()
    state.init_session_state(st, settings)
    registry = load_registry(settings)
    service = _service(settings)

    _submit_pending(settings, service)

    ask, pipeline, sources, evaluation = st.tabs(
        ["Ask", "Pipeline", "Sources", "Evaluation"]
    )
    with ask:
        _render_ask(settings, service, registry)
    with pipeline:
        _render_pipeline(settings)
    with sources:
        _render_sources(registry)
    with evaluation:
        _render_evaluation(settings)


if __name__ == "__main__":
    main()
