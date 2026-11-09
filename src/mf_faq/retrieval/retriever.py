"""Filter, score, dedupe, and select one-page evidence.

Retrieval is the only place that talks to Chroma. It applies the resolved
scheme/field filters, falls back once when a filter returns nothing, rejects
cross-scheme fallback evidence, and selects at most ``final_k`` chunks from one
page with MMR.

See ``implementation.md`` §7.4.
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Sequence

import numpy as np

from mf_faq.config import Settings
from mf_faq.contracts import RetrievedEvidence, RetrievalTrace
from mf_faq.embeddings.embedder import ChunkEmbedder
from mf_faq.index.chroma_store import ChromaStore
from mf_faq.retrieval.resolver import Resolution

__all__ = ["RetrievalError", "RetrievalResult", "Retriever"]

_TOKEN_RE = re.compile(r"\w+|[^\w\s]", re.UNICODE)
_WHITESPACE_RE = re.compile(r"\s+")
_NORMALISE_RE = re.compile(r"[^a-z0-9%]+")


class RetrievalError(RuntimeError):
    """Retrieval could not run (index, model, or Chroma failure)."""


@dataclass
class RetrievalResult:
    evidence: list[RetrievedEvidence]
    trace: RetrievalTrace
    candidates: list[dict[str, Any]] = field(default_factory=list)
    selected_ids: list[str] = field(default_factory=list)
    filter_fallback: bool = False
    page_doc_id: str | None = None
    latency_ms: float = 0.0

    @property
    def found(self) -> bool:
        return bool(self.evidence)


def _estimate_tokens(text: str) -> int:
    return len(_TOKEN_RE.findall(text or ""))


def _normalise_text(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", _NORMALISE_RE.sub(" ", (text or "").lower())).strip()


def _normalise_url(url: str) -> str:
    return (url or "").strip().rstrip("/").lower()


def _where(filters: dict[str, Any]) -> dict[str, Any] | None:
    clauses = [{key: value} for key, value in filters.items() if value is not None]
    if not clauses:
        return None
    if len(clauses) == 1:
        return clauses[0]
    return {"$and": clauses}


class Retriever:
    """Query the persistent collection and return one page of evidence."""

    def __init__(
        self,
        settings: Settings,
        *,
        store: ChromaStore | None = None,
        embedder: ChunkEmbedder | None = None,
        clock=time.perf_counter,
    ) -> None:
        self.settings = settings
        self._store = store
        self._embedder = embedder
        self._clock = clock
        self.corpus_urls = {
            _normalise_url(seed.url) for seed in settings.corpus.seed_pages
        }

    @property
    def store(self) -> ChromaStore:
        if self._store is None:
            self._store = ChromaStore(self.settings)
            self._store.open()
        return self._store

    @property
    def embedder(self) -> ChunkEmbedder:
        if self._embedder is None:
            self._embedder = ChunkEmbedder(self.settings)
        return self._embedder

    def _query(
        self,
        vector: np.ndarray,
        *,
        where: dict[str, Any] | None,
    ) -> dict[str, Any]:
        collection = self.store.collection
        try:
            count = int(collection.count())
        except Exception:
            count = self.settings.retrieval.top_k
        if count <= 0:
            return {"ids": [[]], "documents": [[]], "metadatas": [[]], "distances": [[]], "embeddings": [[]]}
        n_results = min(self.settings.retrieval.top_k, count)
        try:
            return collection.query(
                query_embeddings=np.asarray([vector], dtype=np.float32),
                n_results=n_results,
                where=where,
                include=["documents", "metadatas", "distances", "embeddings"],
            )
        except Exception as exc:
            raise RetrievalError(f"Chroma query failed: {type(exc).__name__}") from None

    @staticmethod
    def _rows(result: dict[str, Any]) -> list[dict[str, Any]]:
        ids = (result.get("ids") or [[]])[0] or []
        documents = (result.get("documents") or [[]])[0] or []
        metadatas = (result.get("metadatas") or [[]])[0] or []
        distances = (result.get("distances") or [[]])[0] or []
        embeddings = result.get("embeddings")
        if embeddings is None:
            query_embeddings: Sequence[Any] = [None] * len(ids)
        else:
            try:
                first = embeddings[0]
            except (IndexError, KeyError, TypeError):
                first = None
            if first is None:
                query_embeddings = [None] * len(ids)
            else:
                query_embeddings = list(first)
        rows: list[dict[str, Any]] = []
        for index, chunk_id in enumerate(ids):
            metadata = dict(metadatas[index] or {}) if index < len(metadatas) else {}
            distance = float(distances[index]) if index < len(distances) else 1.0
            vector = query_embeddings[index] if index < len(query_embeddings) else None
            rows.append(
                {
                    "chunk_id": str(chunk_id),
                    "text": str(documents[index]) if index < len(documents) else "",
                    "metadata": metadata,
                    "distance": distance,
                    "score": 1.0 - distance,
                    "vector": np.asarray(vector, dtype=np.float32) if vector is not None else None,
                }
            )
        return rows

    def _keep(self, row: dict[str, Any], *, scheme_id: str | None) -> bool:
        metadata = row["metadata"]
        if metadata.get("indexed") is not True:
            return False
        if str(metadata.get("content_class", "")) != "factual":
            return False
        if _normalise_url(str(metadata.get("url", ""))) not in self.corpus_urls:
            return False
        if scheme_id and str(metadata.get("scheme_id", "")) != scheme_id:
            return False
        if float(row["score"]) < self.settings.retrieval.score_threshold:
            return False
        if not row["text"].strip():
            return False
        return True

    @staticmethod
    def _dedupe(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        best: dict[tuple[str, str, str], dict[str, Any]] = {}
        order: list[tuple[str, str, str]] = []
        for row in rows:
            metadata = row["metadata"]
            key = (
                str(metadata.get("doc_id", "")),
                str(metadata.get("field", "")),
                _normalise_text(row["text"]),
            )
            if key not in best:
                order.append(key)
                best[key] = row
            elif row["score"] > best[key]["score"]:
                best[key] = row
        return [best[key] for key in order]

    def _mmr(
        self,
        rows: Sequence[dict[str, Any]],
        *,
        limit: int,
        lambda_: float = 0.5,
    ) -> list[dict[str, Any]]:
        remaining = list(rows)
        if not remaining or limit <= 0:
            return []

        vectors = [row.get("vector") for row in remaining]
        if any(vector is None for vector in vectors):
            return sorted(remaining, key=lambda row: row["score"], reverse=True)[:limit]

        selected = [max(remaining, key=lambda row: row["score"])]
        remaining.remove(selected[0])
        while remaining and len(selected) < limit:
            best_row = None
            best_value = float("-inf")
            for row in remaining:
                vector = np.asarray(row["vector"], dtype=np.float32)
                redundancy = max(
                    float(np.dot(vector, np.asarray(chosen["vector"], dtype=np.float32)))
                    for chosen in selected
                )
                value = lambda_ * float(row["score"]) - (1.0 - lambda_) * redundancy
                if value > best_value:
                    best_value = value
                    best_row = row
            if best_row is None:
                break
            selected.append(best_row)
            remaining.remove(best_row)
        return selected

    def _to_evidence(self, row: dict[str, Any]) -> RetrievedEvidence:
        metadata = row["metadata"]
        fetched_at = metadata.get("fetched_at") or None
        return RetrievedEvidence(
            chunk_id=row["chunk_id"],
            doc_id=str(metadata.get("doc_id", "")),
            url=str(metadata.get("url", "")),
            title=str(metadata.get("title", "")),
            section=str(metadata.get("section", "")),
            field=str(metadata.get("field", "other")),
            score=float(row["score"]),
            text=row["text"],
            fetched_at=fetched_at,
        )

    def retrieve(
        self,
        query: str,
        *,
        trace_id: str | None = None,
        intent: str = "",
        scheme_id: str | None = None,
        field: str | None = None,
        resolution: Resolution | None = None,
    ) -> RetrievalResult:
        """Run the §7.4 retrieval pipeline and write-ready trace."""
        started = self._clock()
        trace_id = trace_id or uuid.uuid4().hex
        if resolution is not None:
            scheme_id = resolution.scheme_id
            field = resolution.field

        filters: dict[str, Any] = {"indexed": True}
        if scheme_id:
            filters["scheme_id"] = scheme_id
        if field and field != "other":
            filters["field"] = field

        vector = self.embedder.embed_query(query)
        raw = self._rows(self._query(vector, where=_where(filters)))
        filter_fallback = False

        candidates = [row for row in raw if self._keep(row, scheme_id=scheme_id)]
        if not candidates and (scheme_id or (field and field != "other")):
            filter_fallback = True
            raw = self._rows(self._query(vector, where=_where({"indexed": True})))
            # Never cite another scheme after a fallback.
            candidates = [row for row in raw if self._keep(row, scheme_id=scheme_id)]

        candidates = self._dedupe(candidates)

        if not candidates:
            trace = RetrievalTrace(
                trace_id=trace_id,
                query=query,
                resolved_scheme=scheme_id,
                resolved_field=field,
                intent=intent,
                filters=filters,
                filter_fallback=filter_fallback,
                candidates=[],
                selected=[],
                latency_ms=(self._clock() - started) * 1000.0,
            )
            return RetrievalResult(
                evidence=[],
                trace=trace,
                candidates=[],
                selected_ids=[],
                filter_fallback=filter_fallback,
                page_doc_id=None,
                latency_ms=trace.latency_ms,
            )

        groups: dict[str, list[dict[str, Any]]] = {}
        for row in candidates:
            doc_id = str(row["metadata"].get("doc_id", ""))
            groups.setdefault(doc_id, []).append(row)

        page_doc_id, page_rows = max(
            groups.items(),
            key=lambda item: (
                len(item[1]),
                max(row["score"] for row in item[1]),
            ),
        )
        selected = self._mmr(page_rows, limit=self.settings.retrieval.final_k)
        # §7.5: selected chunks are rendered in descending score order.
        selected.sort(key=lambda row: row["score"], reverse=True)

        budget = self.settings.retrieval.max_context_tokens
        trimmed: list[dict[str, Any]] = []
        used = 0
        for row in selected:
            cost = _estimate_tokens(row["text"])
            if trimmed and used + cost > budget:
                break
            trimmed.append(row)
            used += cost

        evidence = [self._to_evidence(row) for row in trimmed]
        trace_candidates = [
            {
                "chunk_id": row["chunk_id"],
                "doc_id": str(row["metadata"].get("doc_id", "")),
                "scheme_id": str(row["metadata"].get("scheme_id", "")),
                "field": str(row["metadata"].get("field", "")),
                "section": str(row["metadata"].get("section", "")),
                "url": str(row["metadata"].get("url", "")),
                "score": round(float(row["score"]), 6),
            }
            for row in candidates
        ]
        trace = RetrievalTrace(
            trace_id=trace_id,
            query=query,
            resolved_scheme=scheme_id,
            resolved_field=field,
            intent=intent,
            filters=filters,
            filter_fallback=filter_fallback,
            candidates=trace_candidates,
            selected=[item.chunk_id for item in evidence],
            latency_ms=(self._clock() - started) * 1000.0,
        )
        return RetrievalResult(
            evidence=evidence,
            trace=trace,
            candidates=trace_candidates,
            selected_ids=trace.selected,
            filter_fallback=filter_fallback,
            page_doc_id=page_doc_id,
            latency_ms=trace.latency_ms,
        )
