"""Chroma store unit tests: metadata, upsert, filters, and drift guard."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

import chromadb
import numpy as np
import pytest

from mf_faq.config import Settings
from mf_faq.contracts import Chunk, EmbeddingManifest, IndexManifest
from mf_faq.embeddings.embedder import EXPECTED_DIMENSION
from mf_faq.index.chroma_store import (
    ChromaStore,
    IndexDriftError,
    IndexingError,
    flatten_metadata_value,
    metadata_for_chunk,
)

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "config" / "config.yaml"
NOW = datetime(2026, 11, 8, 10, 0, tzinfo=timezone.utc)


def make_chunk(
    chunk_id: str,
    *,
    scheme_id: str = "hdfc_elss",
    field: str = "expense_ratio",
    page_type: str | None = "scheme",
    section_path: list[str] | None = None,
    fetched_at: datetime | None = NOW,
    indexed: bool = True,
) -> Chunk:
    text = f"{chunk_id}: fact"
    return Chunk(
        chunk_id=chunk_id,
        doc_id=f"doc_{scheme_id}",
        url=f"https://groww.in/mutual-funds/{scheme_id}",
        title=f"HDFC {scheme_id}",
        scheme_id=scheme_id,
        page_type=page_type,
        section_path=section_path if section_path is not None else ["Fund details", field],
        field=field,
        chunk_type="kv_pair",
        content_class="factual" if indexed else "performance",
        indexed=indexed,
        exclusion_reason=None if indexed else "performance content",
        text=text,
        token_count=5,
        content_hash=f"sha256:{hashlib.sha256(text.encode()).hexdigest()}",
        fetched_at=fetched_at,
    )


def make_vectors(count: int) -> np.ndarray:
    rng = np.random.default_rng(7)
    vectors = rng.standard_normal((count, EXPECTED_DIMENSION)).astype(np.float32)
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    return vectors


def embedding_manifest(settings: Settings, **overrides) -> EmbeddingManifest:
    values = dict(
        model=settings.embedding.model,
        dimension=EXPECTED_DIMENSION,
        max_seq_length=settings.embedding.max_seq_length,
        count=4,
        normalized=settings.embedding.normalize,
        config_hash=settings.config_hash,
        created_at=NOW,
    )
    values.update(overrides)
    return EmbeddingManifest(**values)


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings.load(CONFIG, base_dir=tmp_path)


@pytest.fixture()
def store(settings: Settings) -> ChromaStore:
    created = ChromaStore(settings, client=chromadb.EphemeralClient())
    created.drop()  # ephemeral clients share one in-process store
    return created


def test_metadata_flattening_handles_none_lists_and_booleans() -> None:
    chunk = make_chunk(
        "chunk_a",
        page_type=None,
        section_path=["Fund details", "Expense ratio"],
    )

    metadata = metadata_for_chunk(chunk)

    assert metadata["section"] == "Fund details > Expense ratio"
    assert metadata["page_type"] == ""
    assert metadata["indexed"] is True
    assert metadata["fetched_at"] == NOW.isoformat()
    assert metadata["field"] == "expense_ratio"
    assert flatten_metadata_value(None) == ""
    assert flatten_metadata_value(["a", "b"]) == "a > b"
    assert flatten_metadata_value(True) is True
    assert flatten_metadata_value(1.5) == 1.5


def test_count_equals_upserted_record_count(store: ChromaStore) -> None:
    chunks = [make_chunk(f"chunk_{index}") for index in range(4)]
    store.open()

    assert store.count() == 0
    assert store.upsert(chunks, make_vectors(len(chunks))) == len(chunks)
    assert store.count() == len(chunks)
    assert set(store.all_ids()) == {chunk.chunk_id for chunk in chunks}


def test_double_upsert_keeps_count_stable(store: ChromaStore) -> None:
    chunks = [make_chunk(f"chunk_{index}") for index in range(4)]
    vectors = make_vectors(len(chunks))
    store.open()

    store.upsert(chunks, vectors)
    first = store.count()
    store.upsert(chunks, vectors)

    assert store.count() == first == len(chunks)


def test_filter_by_scheme_returns_only_that_scheme(store: ChromaStore) -> None:
    chunks = [
        make_chunk("a", scheme_id="hdfc_elss"),
        make_chunk("b", scheme_id="hdfc_large_cap"),
        make_chunk("c", scheme_id="hdfc_elss", field="exit_load"),
        make_chunk("d", scheme_id="hdfc_mid_cap"),
    ]
    store.open()
    store.upsert(chunks, make_vectors(len(chunks)))

    assert set(store.ids_where({"scheme_id": "hdfc_elss"})) == {"a", "c"}
    assert set(store.ids_where({"scheme_id": "hdfc_mid_cap"})) == {"d"}


def test_filter_by_field_returns_only_that_field(store: ChromaStore) -> None:
    chunks = [
        make_chunk("a", field="expense_ratio"),
        make_chunk("b", field="exit_load"),
        make_chunk("c", field="expense_ratio", scheme_id="hdfc_large_cap"),
        make_chunk("d", field="lock_in"),
    ]
    store.open()
    store.upsert(chunks, make_vectors(len(chunks)))

    assert set(store.ids_where({"field": "expense_ratio"})) == {"a", "c"}
    assert set(store.ids_where({"field": "lock_in"})) == {"d"}


def test_drift_guard_raises_on_model_mismatch(store: ChromaStore, settings: Settings) -> None:
    store.open()

    with pytest.raises(IndexDriftError, match="model"):
        store.check_drift(embedding_manifest(settings, model="some/other-model"))


def test_drift_guard_raises_on_dimension_mismatch(
    store: ChromaStore, settings: Settings
) -> None:
    store.open()

    with pytest.raises(IndexDriftError, match="dimension"):
        store.check_drift(embedding_manifest(settings, dimension=768))


def test_drift_guard_raises_on_manifest_count_mismatch(
    store: ChromaStore, settings: Settings
) -> None:
    chunks = [make_chunk("a"), make_chunk("b")]
    store.open()
    store.upsert(chunks, make_vectors(len(chunks)))

    index_manifest = IndexManifest(
        collection=settings.vector_db.collection,
        count=99,
        model=settings.embedding.model,
        dimension=EXPECTED_DIMENSION,
        distance=settings.vector_db.distance,
        config_hash=settings.config_hash,
        created_at=NOW,
    )

    with pytest.raises(IndexDriftError, match="count"):
        store.check_drift(embedding_manifest(settings, count=2), index_manifest)


def test_non_indexed_chunks_are_rejected(store: ChromaStore) -> None:
    chunk = make_chunk("excluded", indexed=False)
    store.open()

    with pytest.raises(IndexingError, match="only indexed"):
        store.upsert([chunk], make_vectors(1))
