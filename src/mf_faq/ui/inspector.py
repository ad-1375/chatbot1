"""Read-only pipeline inspector for Load, Chunk, Embed, Index, Retrieve, Answer.

Every sub-tab reads artifacts produced by Steps 1–5 and never writes, rebuilds,
or fetches anything. Missing files render an explicit "artifact not found"
message with the path an operator should inspect.

See ``implementation.md`` §8.3.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

import numpy as np
import streamlit as st

from mf_faq.config import Settings
from mf_faq.index.chroma_store import ChromaStore
from mf_faq.io_utils import read_json, read_jsonl

__all__ = ["render"]

_MAX_ROWS = 250
_SMOKE_FILTERS: tuple[dict[str, Any], ...] = (
    {"scheme_id": "hdfc_elss"},
    {"field": "expense_ratio"},
    {"indexed": True},
)


def _missing(path: Path) -> None:
    st.warning(f"artifact not found: {path}")


def _json(path: Path) -> Any | None:
    if not path.is_file():
        _missing(path)
        return None
    try:
        return read_json(path)
    except (OSError, ValueError) as exc:
        st.error(f"artifact unreadable: {path}")
        st.caption(str(exc))
        return None


def _jsonl(path: Path, *, limit: int | None = None) -> list[dict[str, Any]] | None:
    if not path.is_file():
        _missing(path)
        return None
    rows: list[dict[str, Any]] = []
    try:
        for row in read_jsonl(path):
            rows.append(row)
            if limit is not None and len(rows) >= limit:
                break
    except (OSError, ValueError) as exc:
        st.error(f"artifact unreadable: {path}")
        st.caption(str(exc))
        return None
    return rows


def _table(rows: Iterable[dict[str, Any]]) -> None:
    values = list(rows)
    if values:
        st.dataframe(values, hide_index=True, width="stretch")
    else:
        st.caption("No rows to display.")


def _text_preview(text: str, limit: int = 6000) -> str:
    return text if len(text) <= limit else f"{text[:limit]}\n\n[truncated]"


# --------------------------------------------------------------------- Load
def _render_load(settings: Settings) -> None:
    st.subheader("Load")
    manifest_path = settings.resolved_path("data/manifests/load_manifest.json")
    manifest = _json(manifest_path)
    if manifest is None:
        return

    st.caption(
        f"Created {manifest.get('created_at', 'unknown')} · "
        f"config {manifest.get('config_hash', 'unknown')} · "
        f"failures {manifest.get('failure_count', 0)}"
    )
    documents = list(manifest.get("documents") or [])
    coverage_keys = sorted(
        {
            key
            for document in documents
            for key in (document.get("coverage") or {})
        }
    )
    rows = []
    for document in documents:
        coverage = document.get("coverage") or {}
        row = {
            "doc_id": document.get("doc_id", ""),
            "status": document.get("http_status", ""),
            "renderer": document.get("renderer", ""),
            "fetched_at": document.get("fetched_at", ""),
            "words": document.get("word_count", ""),
        }
        row.update({key: "yes" if coverage.get(key) else "-" for key in coverage_keys})
        rows.append(row)
    _table(rows)

    documents_path = settings.resolved_path("data/processed/documents.jsonl")
    processed = _jsonl(documents_path)
    if processed is None:
        return

    by_id = {str(item.get("doc_id")): item for item in processed}
    options = [str(item.get("doc_id")) for item in processed]
    if not options:
        st.caption("No cleaned documents available.")
        return
    selected = st.selectbox("Cleaned text", options, key="load_doc_select")
    document = by_id.get(selected, {})
    st.text_area(
        "Cleaned document text (read-only)",
        value=_text_preview(str(document.get("text", ""))),
        height=320,
        disabled=True,
        key="load_cleaned_text",
    )

    raw_relative = next(
        (
            item.get("raw_path")
            for item in documents
            if str(item.get("doc_id")) == selected and item.get("raw_path")
        ),
        None,
    )
    if not raw_relative:
        st.warning("raw HTML snapshot path is not recorded for this page")
        return
    raw_path = settings.resolved_path(str(raw_relative))
    if not raw_path.is_file():
        _missing(raw_path)
        return
    try:
        raw = raw_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        st.error(f"artifact unreadable: {raw_path}")
        st.caption(str(exc))
        return
    st.caption("Raw HTML snippet (first 2 KB, escaped)")
    st.code(raw[:2048], language="html", wrap_lines=True)


# -------------------------------------------------------------------- Chunk
def _section_paths(rows: Iterable[dict[str, Any]], doc_id: str) -> list[str]:
    paths = set()
    for row in rows:
        if doc_id != "all" and row.get("doc_id") != doc_id:
            continue
        section = row.get("section_path") or []
        if isinstance(section, list) and section:
            paths.add(" > ".join(str(item) for item in section))
    return sorted(paths)


def _render_chunk(settings: Settings) -> None:
    st.subheader("Chunk")
    chunks_path = settings.resolved_path("data/chunks/chunks.jsonl")
    chunks = _jsonl(chunks_path)
    if chunks is None:
        return
    excluded_path = settings.resolved_path("data/chunks/excluded.jsonl")
    excluded = _jsonl(excluded_path)

    stats_path = settings.resolved_path("data/chunks/chunk_stats.json")
    stats = _json(stats_path)
    if stats is not None:
        st.caption(
            f"{stats.get('chunk_count', len(chunks))} chunks · "
            f"{stats.get('indexed_count', 0)} indexed · "
            f"{stats.get('excluded_count', 0)} excluded · "
            f"invariant violations {stats.get('invariant_violations', 0)}"
        )
        tokens = stats.get("tokens") or {}
        if tokens:
            st.write(
                f"Tokens: min {tokens.get('min')} · mean {tokens.get('mean')} · "
                f"max {tokens.get('max')} · total {tokens.get('total')}"
            )
        st.json(
            {
                "by_content_class": stats.get("by_content_class", {}),
                "by_field": stats.get("by_field", {}),
                "by_chunk_type": stats.get("by_chunk_type", {}),
            }
        )

    doc_ids = sorted({str(row.get("doc_id")) for row in chunks})
    left, right = st.columns(2)
    doc_filter = left.selectbox(
        "Page", ["all", *doc_ids], key="chunk_doc_filter"
    )
    query_filter = right.text_input(
        "Filter field or text", key="chunk_text_filter"
    ).strip().lower()

    filtered = [
        row
        for row in chunks
        if (doc_filter == "all" or row.get("doc_id") == doc_filter)
        and (
            not query_filter
            or query_filter in str(row.get("field", "")).lower()
            or query_filter in str(row.get("text", "")).lower()
        )
    ]
    st.caption(f"{len(filtered)} matching chunks (showing up to {_MAX_ROWS})")
    _table(
        {
            "chunk_id": row.get("chunk_id"),
            "doc_id": row.get("doc_id"),
            "field": row.get("field"),
            "chunk_type": row.get("chunk_type"),
            "content_class": row.get("content_class"),
            "tokens": row.get("token_count"),
            "indexed": row.get("indexed"),
            "text": _text_preview(str(row.get("text", "")), 180),
        }
        for row in filtered[:_MAX_ROWS]
    )

    st.caption("Section tree")
    st.json(_section_paths(chunks, doc_filter))

    st.caption("Excluded chunks")
    if excluded is None:
        return
    excluded_filtered = [
        row
        for row in excluded
        if doc_filter == "all" or row.get("doc_id") == doc_filter
    ]
    _table(
        {
            "chunk_id": row.get("chunk_id"),
            "doc_id": row.get("doc_id"),
            "field": row.get("field"),
            "reason": row.get("exclusion_reason"),
            "content_class": row.get("content_class"),
        }
        for row in excluded_filtered[:_MAX_ROWS]
    )


# -------------------------------------------------------------------- Embed
def _render_embed(settings: Settings) -> None:
    st.subheader("Embed")
    manifest_path = settings.resolved_path(
        "data/embeddings/embedding_manifest.json"
    )
    manifest = _json(manifest_path)
    if manifest is None:
        return

    st.write(
        f"Model: {manifest.get('model')} · dimension {manifest.get('dimension')} · "
        f"count {manifest.get('count')} · normalized {manifest.get('normalized')}"
    )
    st.caption(
        f"Max sequence length {manifest.get('max_seq_length')} · "
        f"config {manifest.get('config_hash')} · "
        f"created {manifest.get('created_at')}"
    )

    matrix_path = settings.resolved_path("data/embeddings/embeddings.npy")
    if not matrix_path.is_file():
        _missing(matrix_path)
    else:
        try:
            matrix = np.load(matrix_path)
            norms = np.linalg.norm(matrix, axis=1)
            st.write(
                f"Matrix shape {matrix.shape} · dtype {matrix.dtype} · "
                f"finite {bool(np.isfinite(matrix).all())} · "
                f"norm min {float(norms.min()):.6f} · "
                f"max {float(norms.max()):.6f} · "
                f"max deviation {float(np.abs(norms - 1.0).max()):.6f}"
            )
        except (OSError, ValueError) as exc:
            st.error(f"artifact unreadable: {matrix_path}")
            st.caption(str(exc))

    preview_path = settings.resolved_path(
        "data/embeddings/embedding_preview.json"
    )
    preview = _json(preview_path)
    if preview is None:
        return
    st.caption("First eight dimensions of three vectors")
    st.json(list(preview.get("preview") or [])[:3])


# -------------------------------------------------------------------- Index
def _open_store(settings: Settings, injected: Any | None) -> Any | None:
    if injected is not None:
        return injected
    manifest_path = settings.resolved_path("data/index_manifest.json")
    persist_dir = settings.resolved_path(settings.vector_db.persist_dir)
    if not manifest_path.is_file() or not persist_dir.is_dir():
        return None
    store = ChromaStore(settings)
    try:
        names = {
            getattr(collection, "name", str(collection))
            for collection in store.client.list_collections()
        }
    except Exception:
        return None
    if store.collection_name not in names:
        return None
    try:
        store.open()
    except Exception:
        return None
    return store


def _render_index(settings: Settings, injected: Any | None) -> None:
    st.subheader("Index")
    manifest_path = settings.resolved_path("data/index_manifest.json")
    manifest = _json(manifest_path)
    if manifest is None:
        return
    st.write(
        f"Collection {manifest.get('collection')} · count {manifest.get('count')} · "
        f"distance {manifest.get('distance')} · model {manifest.get('model')} · "
        f"dimension {manifest.get('dimension')}"
    )
    st.caption(
        f"Config {manifest.get('config_hash')} · created {manifest.get('created_at')}"
    )

    store = _open_store(settings, injected)
    if store is None:
        st.warning(
            "index collection unavailable: "
            f"{settings.resolved_path(settings.vector_db.persist_dir)}"
        )
        return

    try:
        st.caption(f"Live collection count: {store.count()}")
        sample = store.collection.get(limit=3, include=["metadatas", "documents"])
    except Exception as exc:
        st.error("index collection could not be read")
        st.caption(str(exc))
        return

    ids = list(sample.get("ids") or [])
    metadatas = list(sample.get("metadatas") or [])
    documents = list(sample.get("documents") or [])
    rows = []
    for index, chunk_id in enumerate(ids):
        metadata = metadatas[index] if index < len(metadatas) else {}
        text = documents[index] if index < len(documents) else ""
        rows.append(
            {
                "chunk_id": chunk_id,
                "doc_id": metadata.get("doc_id"),
                "scheme_id": metadata.get("scheme_id"),
                "field": metadata.get("field"),
                "section": metadata.get("section"),
                "text": _text_preview(str(text), 160),
            }
        )
    st.caption("Metadata sample")
    _table(rows)

    st.caption("Filter smoke results")
    smoke_rows = []
    for where in _SMOKE_FILTERS:
        try:
            matches = store.ids_where(where=where)
            smoke_rows.append(
                {
                    "filter": str(where),
                    "count": len(matches),
                    "sample_ids": ", ".join(matches[:3]),
                }
            )
        except Exception as exc:
            smoke_rows.append(
                {"filter": str(where), "count": "error", "sample_ids": str(exc)}
            )
    _table(smoke_rows)


# ----------------------------------------------------------------- Retrieve
def _latest_trace(directory: Path, preferred: str | None) -> Path | None:
    if preferred:
        preferred_path = directory / f"{preferred}.json"
        if preferred_path.is_file():
            return preferred_path
    if not directory.is_dir():
        return None
    files = sorted(
        directory.glob("*.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return files[0] if files else None


def _chunk_texts(settings: Settings) -> dict[str, str]:
    path = settings.resolved_path("data/chunks/chunks.jsonl")
    if not path.is_file():
        return {}
    texts: dict[str, str] = {}
    try:
        for row in read_jsonl(path):
            texts[str(row.get("chunk_id"))] = str(row.get("text", ""))
    except (OSError, ValueError):
        return {}
    return texts


def _render_retrieve(settings: Settings, latest_trace_id: str | None) -> None:
    st.subheader("Retrieve")
    directory = settings.resolved_path("data/traces/retrieval")
    trace_path = _latest_trace(directory, latest_trace_id)
    if trace_path is None:
        _missing(directory)
        return
    trace = _json(trace_path)
    if trace is None:
        return

    st.caption(f"Trace {trace.get('trace_id')} · {trace_path.name}")
    st.write(
        f"Query: {trace.get('query', '')}\n\n"
        f"Resolved scheme: {trace.get('resolved_scheme')} · "
        f"field: {trace.get('resolved_field')} · intent: {trace.get('intent')} · "
        f"filter fallback: {trace.get('filter_fallback')}"
    )
    st.json(trace.get("filters") or {})

    selected = {str(item) for item in (trace.get("selected") or [])}
    candidates = list(trace.get("candidates") or [])
    st.caption(f"Top {min(12, len(candidates))} candidates")
    _table(
        {
            "chunk_id": row.get("chunk_id"),
            "doc_id": row.get("doc_id"),
            "scheme_id": row.get("scheme_id"),
            "field": row.get("field"),
            "section": row.get("section"),
            "score": row.get("score"),
            "selected": str(row.get("chunk_id")) in selected,
        }
        for row in candidates[:12]
    )

    st.caption("Selected context")
    if not selected:
        st.caption("No chunks were selected.")
        return
    texts = _chunk_texts(settings)
    for chunk_id in sorted(selected):
        with st.expander(chunk_id, expanded=False):
            st.code(texts.get(chunk_id, "chunk text not found in chunks.jsonl"))


# ------------------------------------------------------------------- Answer
def _render_answer(settings: Settings, latest_trace_id: str | None) -> None:
    st.subheader("Answer")
    directory = settings.resolved_path("data/traces/answers")
    trace_path = _latest_trace(directory, latest_trace_id)
    if trace_path is None:
        _missing(directory)
        return
    trace = _json(trace_path)
    if trace is None:
        return
    trace_id = str(trace.get("trace_id") or trace_path.stem)

    st.caption(f"Trace {trace_id} · status {trace.get('status')}")
    st.write(
        f"Provider {trace.get('provider') or 'n/a'} · "
        f"model {trace.get('model') or 'n/a'} · "
        f"fallback {trace.get('fallback')} · "
        f"repair attempted {trace.get('repair_attempted')} · "
        f"latency {trace.get('latency_ms', 0):.1f} ms"
    )
    if trace.get("answer"):
        st.markdown(
            str(trace.get("answer")).replace("<", "&lt;").replace(">", "&gt;"),
            unsafe_allow_html=False,
        )
    st.json(
        {
            "status": trace.get("status"),
            "citation_url": trace.get("citation_url"),
            "evidence_ids": trace.get("evidence_ids"),
            "guardrails": trace.get("guardrails"),
            "usage": trace.get("usage"),
        }
    )

    timings = trace.get("stage_timings_ms") or {}
    if timings:
        _table({"stage": key, "ms": value} for key, value in timings.items())

    prompt_relative = trace.get("prompt_path")
    prompt_path = (
        settings.resolved_path(str(prompt_relative))
        if prompt_relative
        else settings.resolved_path(f"data/traces/prompts/{trace_id}.txt")
    )
    st.caption("Prompt")
    if prompt_path.is_file():
        st.code(
            prompt_path.read_text(encoding="utf-8", errors="replace"),
            language=None,
            wrap_lines=True,
        )
    else:
        _missing(prompt_path)

    response_path = settings.resolved_path(f"data/traces/responses/{trace_id}.txt")
    st.caption("Raw model response")
    if response_path.is_file():
        st.code(
            response_path.read_text(encoding="utf-8", errors="replace"),
            language=None,
            wrap_lines=True,
        )
    else:
        st.caption(
            "raw response not stored (PII-safe or generation did not run); "
            f"expected {response_path}"
        )


# ------------------------------------------------------------------- render
def render(
    st_module: Any,
    settings: Settings,
    *,
    store: Any | None = None,
    latest_trace_id: str | None = None,
) -> None:
    """Render the six read-only sub-tabs. ``store`` is an optional test seam."""
    del st_module  # Streamlit calls use the module-level ``st`` API
    tabs = st.tabs(["Load", "Chunk", "Embed", "Index", "Retrieve", "Answer"])
    with tabs[0]:
        _render_load(settings)
    with tabs[1]:
        _render_chunk(settings)
    with tabs[2]:
        _render_embed(settings)
    with tabs[3]:
        _render_index(settings, store)
    with tabs[4]:
        _render_retrieve(settings, latest_trace_id)
    with tabs[5]:
        _render_answer(settings, latest_trace_id)
