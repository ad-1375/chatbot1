"""Retrieval filters, threshold, dedupe, MMR, and one-page tests (§7.4)."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

import chromadb
import numpy as np
import pytest

from mf_faq.config import Settings
from mf_faq.contracts import Chunk
from mf_faq.embeddings.embedder import EXPECTED_DIMENSION
from mf_faq.index.chroma_store import ChromaStore
from mf_faq.retrieval.retriever import Retriever

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "config" / "config.yaml"
NOW = datetime(2026, 11, 9, 10, 0, tzinfo=timezone.utc)

SCHEMES = {
    "hdfc_elss": (
        "c4_hdfc_elss",
        "https://groww.in/mutual-funds/hdfc-elss-tax-saver-fund-direct-plan-growth",
        "HDFC ELSS Tax Saver Fund Direct Plan Growth",
    ),
    "hdfc_flexi_cap": (
        "c3_hdfc_flexi_cap",
        "https://groww.in/mutual-funds/hdfc-equity-fund-direct-growth",
        "HDFC Flexi Cap Direct Plan Growth",
    ),
    "hdfc_large_cap": (
        "c2_hdfc_large_cap",
        "https://groww.in/mutual-funds/hdfc-large-cap-fund-direct-growth",
        "HDFC Large Cap Fund Direct Growth",
    ),
}


def basis(index: int) -> np.ndarray:
    vector = np.zeros(EXPECTED_DIMENSION, dtype=np.float32)
    vector[index] = 1.0
    return vector


def blend(primary: int, secondary: int, weight: float) -> np.ndarray:
    vector = basis(primary) + np.float32(weight) * basis(secondary)
    return (vector / np.linalg.norm(vector)).astype(np.float32)


def make_chunk(
    chunk_id: str,
    *,
    scheme: str = "hdfc_elss",
    field: str = "expense_ratio",
    text: str | None = None,
    content_class: str = "factual",
    url: str | None = None,
) -> Chunk:
    doc_id, corpus_url, title = SCHEMES[scheme]
    body = text or f"{title} > {field}: fact {chunk_id}"
    return Chunk(
        chunk_id=chunk_id,
        doc_id=doc_id,
        url=url or corpus_url,
        title=title,
        scheme_id=scheme,
        page_type="scheme",
        section_path=["Fund details", field],
        field=field,
        chunk_type="kv_pair",
        content_class=content_class,
        indexed=True,
        exclusion_reason=None,
        text=body,
        token_count=max(1, len(body.split())),
        content_hash=f"sha256:{hashlib.sha256(body.encode()).hexdigest()}",
        fetched_at=NOW,
    )


class FakeEmbedder:
    def __init__(self, vector: np.ndarray) -> None:
        self.vector = np.asarray(vector, dtype=np.float32)
        self.calls = 0

    def embed_query(self, query: str) -> np.ndarray:
        self.calls += 1
        return self.vector


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings.load(CONFIG, base_dir=tmp_path)


@pytest.fixture()
def store(settings: Settings) -> ChromaStore:
    created = ChromaStore(settings, client=chromadb.EphemeralClient())
    created.drop()
    created.open()
    return created


def upsert(store: ChromaStore, rows: list[tuple[Chunk, np.ndarray]]) -> None:
    chunks = [row[0] for row in rows]
    vectors = np.vstack([row[1] for row in rows])
    store.upsert(chunks, vectors)


def retrieve(store: ChromaStore, *, query_vector: np.ndarray, **kwargs):
    retriever = Retriever(store.settings, store=store, embedder=FakeEmbedder(query_vector))
    return retriever.retrieve("fixture query", **kwargs)


def test_threshold_drops_low_score(store: ChromaStore) -> None:
    upsert(store, [(make_chunk("low"), basis(1))])

    result = retrieve(
        store,
        query_vector=basis(0),
        scheme_id="hdfc_elss",
        field="expense_ratio",
    )

    assert result.found is False
    assert result.evidence == []
    assert result.trace.selected == []


def test_dedupe_keeps_the_higher_scoring_duplicate(store: ChromaStore) -> None:
    text = "HDFC ELSS Tax Saver Fund Direct Plan Growth > Expense ratio: 1.20%"
    upsert(
        store,
        [
            (make_chunk("best", text=text), basis(0)),
            (make_chunk("worse", text=text), blend(0, 1, 0.5)),
        ],
    )

    result = retrieve(
        store,
        query_vector=basis(0),
        scheme_id="hdfc_elss",
        field="expense_ratio",
    )

    assert len(result.evidence) == 1
    assert result.evidence[0].chunk_id == "best"
    assert result.evidence[0].score == pytest.approx(1.0, abs=1e-5)


def test_one_page_rule_keeps_a_single_doc(store: ChromaStore) -> None:
    upsert(
        store,
        [
            (make_chunk("elss_a", field="expense_ratio"), blend(0, 1, 0.1)),
            (make_chunk("elss_b", field="exit_load"), blend(0, 2, 0.2)),
            (make_chunk("flexi", scheme="hdfc_flexi_cap", field="benchmark"), blend(0, 3, 0.3)),
        ],
    )

    result = retrieve(store, query_vector=basis(0))

    assert result.found is True
    assert len(result.evidence) == 2
    assert {item.doc_id for item in result.evidence} == {"c4_hdfc_elss"}
    assert result.page_doc_id == "c4_hdfc_elss"


def test_mmr_keeps_at_most_final_k(store: ChromaStore) -> None:
    rows = [
        (
            make_chunk(f"chunk_{index}", text=f"fact number {index}"),
            blend(0, index + 1, 0.02 * index),
        )
        for index in range(7)
    ]
    upsert(store, rows)

    result = retrieve(
        store,
        query_vector=basis(0),
        scheme_id="hdfc_elss",
        field="expense_ratio",
    )

    assert len(result.evidence) == store.settings.retrieval.final_k
    assert len(result.trace.selected) == len(result.evidence)


def test_filter_fallback_keeps_same_scheme_evidence(store: ChromaStore) -> None:
    upsert(store, [(make_chunk("expense"), basis(0))])

    result = retrieve(
        store,
        query_vector=basis(0),
        scheme_id="hdfc_elss",
        field="benchmark",
    )

    assert result.filter_fallback is True
    assert result.found is True
    assert result.evidence[0].field == "expense_ratio"
    assert result.trace.filter_fallback is True


def test_cross_scheme_fallback_is_rejected(store: ChromaStore) -> None:
    upsert(
        store,
        [
            (
                make_chunk(
                    "flexi_benchmark",
                    scheme="hdfc_flexi_cap",
                    field="benchmark",
                ),
                basis(0),
            )
        ],
    )

    result = retrieve(
        store,
        query_vector=basis(0),
        scheme_id="hdfc_elss",
        field="benchmark",
    )

    assert result.filter_fallback is True
    assert result.found is False
    assert result.evidence == []
    assert result.page_doc_id is None


def test_zero_records_returns_not_found_shape(store: ChromaStore) -> None:
    result = retrieve(
        store,
        query_vector=basis(0),
        scheme_id="hdfc_elss",
        field="expense_ratio",
    )

    assert result.found is False
    assert result.evidence == []
    assert result.trace.selected == []
    assert result.trace.candidates == []


def test_non_factual_and_off_allowlist_records_are_excluded(store: ChromaStore) -> None:
    upsert(
        store,
        [
            (
                make_chunk("performance", content_class="performance"),
                basis(0),
            ),
            (
                make_chunk(
                    "off_allowlist",
                    url="https://evil.example/hdfc-elss",
                ),
                blend(0, 1, 0.1),
            ),
        ],
    )

    result = retrieve(
        store,
        query_vector=basis(0),
        scheme_id="hdfc_elss",
        field="expense_ratio",
    )

    assert result.found is False
    assert result.evidence == []


def test_evidence_is_sorted_by_score_and_trace_matches(store: ChromaStore) -> None:
    upsert(
        store,
        [
            (make_chunk("high"), basis(0)),
            (make_chunk("mid"), blend(0, 1, 0.5)),
            (make_chunk("low"), blend(0, 2, 1.0)),
        ],
    )

    result = retrieve(
        store,
        query_vector=basis(0),
        scheme_id="hdfc_elss",
        field="expense_ratio",
    )

    scores = [item.score for item in result.evidence]
    assert scores == sorted(scores, reverse=True)
    assert result.trace.selected == [item.chunk_id for item in result.evidence]
    assert result.trace.filters["scheme_id"] == "hdfc_elss"
    assert result.trace.filters["field"] == "expense_ratio"
