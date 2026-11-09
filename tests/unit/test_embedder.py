"""Embedder unit tests with a deterministic 384-d fake encoder."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import numpy as np
import pytest

from mf_faq.config import Settings
from mf_faq.contracts import Chunk
from mf_faq.embeddings.embedder import (
    EMBEDDING_MODEL_PATH_ENV,
    EXPECTED_DIMENSION,
    ChunkEmbedder,
    EmbeddingError,
    embedding_model_source,
    validate_embeddings,
)

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "config" / "config.yaml"
NOW = datetime(2026, 11, 8, 10, 0, tzinfo=timezone.utc)


class FakeEncoder:
    """Deterministic 384-d encoder that records every encode call."""

    def __init__(self, dimension: int = EXPECTED_DIMENSION) -> None:
        self.dimension = dimension
        self.calls: list[list[str]] = []

    def get_sentence_embedding_dimension(self) -> int:
        return self.dimension

    def encode(self, inputs: Sequence[str], **kwargs: Any) -> np.ndarray:
        self.calls.append(list(inputs))
        rows = []
        for text in inputs:
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            seed = int.from_bytes(digest[:8], "big")
            vector = np.random.default_rng(seed).standard_normal(self.dimension)
            vector = vector.astype(np.float32)
            if kwargs.get("normalize_embeddings"):
                vector = vector / np.linalg.norm(vector)
            rows.append(vector)
        return np.vstack(rows).astype(np.float32)


def make_chunk(
    chunk_id: str,
    text: str,
    *,
    content_hash: str | None = None,
    indexed: bool = True,
) -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        doc_id="c4_hdfc_elss",
        url="https://groww.in/mutual-funds/hdfc-elss-tax-saver-fund-direct-plan-growth",
        title="HDFC ELSS Tax Saver Fund Direct Plan Growth",
        scheme_id="hdfc_elss",
        page_type="scheme",
        section_path=["Fund details", "Expense ratio"],
        field="expense_ratio",
        chunk_type="kv_pair",
        content_class="factual",
        indexed=indexed,
        exclusion_reason=None if indexed else "performance content",
        text=text,
        token_count=12,
        content_hash=content_hash
        or f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}",
        fetched_at=NOW,
    )


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings.load(CONFIG, base_dir=tmp_path)


@pytest.fixture()
def encoder() -> FakeEncoder:
    return FakeEncoder()


@pytest.fixture()
def chunks() -> list[Chunk]:
    return [
        make_chunk("chunk_a", "Expense ratio: 1.20%"),
        make_chunk("chunk_b", "Exit load: 1% within 1 year"),
        make_chunk("chunk_c", "Minimum SIP: INR 500"),
    ]


def test_dimension_is_384(settings: Settings, encoder: FakeEncoder, chunks: list[Chunk]) -> None:
    embedder = ChunkEmbedder(settings, model=encoder)
    batch = embedder.embed_documents(chunks)

    assert embedder.dimension == EXPECTED_DIMENSION == 384
    assert batch.vectors.shape == (len(chunks), 384)
    assert encoder.get_sentence_embedding_dimension() == 384


def test_vectors_are_float32(settings: Settings, encoder: FakeEncoder, chunks: list[Chunk]) -> None:
    batch = ChunkEmbedder(settings, model=encoder).embed_documents(chunks)

    assert batch.vectors.dtype == np.float32


def test_normalized_vectors_have_unit_norm(
    settings: Settings, encoder: FakeEncoder, chunks: list[Chunk]
) -> None:
    batch = ChunkEmbedder(settings, model=encoder).embed_documents(chunks)
    norms = np.linalg.norm(batch.vectors, axis=1)

    np.testing.assert_allclose(norms, np.ones(len(chunks)), atol=1e-3)


def test_no_nan_or_inf(settings: Settings, encoder: FakeEncoder, chunks: list[Chunk]) -> None:
    batch = ChunkEmbedder(settings, model=encoder).embed_documents(chunks)

    assert np.isfinite(batch.vectors).all()
    assert validate_embeddings(batch.vectors, batch.chunk_ids) == []


def test_query_and_documents_use_the_same_model(
    settings: Settings, encoder: FakeEncoder
) -> None:
    chunk = make_chunk("chunk_a", "Expense ratio: 1.20%")
    embedder = ChunkEmbedder(settings, model=encoder)

    document = embedder.embed_documents([chunk]).vectors[0]
    query = embedder.embed_query(chunk.text)

    assert embedder.model is encoder
    assert len(encoder.calls) == 2
    assert encoder.calls[-1] == [chunk.text]
    np.testing.assert_allclose(query, document, rtol=0, atol=1e-6)


def test_cache_hit_avoids_reencoding(
    settings: Settings, encoder: FakeEncoder, chunks: list[Chunk]
) -> None:
    embedder = ChunkEmbedder(settings, model=encoder)

    first = embedder.embed_documents(chunks)
    second = embedder.embed_documents(chunks)

    assert first.encoded_count == len(chunks)
    assert second.encoded_count == 0
    assert second.cache_hits == len(chunks)
    assert len(encoder.calls) == 1
    np.testing.assert_array_equal(first.vectors, second.vectors)


def test_duplicate_content_is_encoded_once(
    settings: Settings, encoder: FakeEncoder
) -> None:
    text = "Expense ratio: 1.20%"
    duplicate = make_chunk("chunk_duplicate", text)
    original = make_chunk("chunk_original", text)
    embedder = ChunkEmbedder(settings, model=encoder)

    batch = embedder.embed_documents([original, duplicate])

    assert batch.encoded_count == 1
    assert batch.cache_hits == 1
    assert encoder.calls == [[text]]
    np.testing.assert_array_equal(batch.vectors[0], batch.vectors[1])


def test_wrong_dimension_model_is_rejected(settings: Settings) -> None:
    with pytest.raises(EmbeddingError, match="dimension 128"):
        ChunkEmbedder(settings, model=FakeEncoder(dimension=128))


def test_blank_query_is_rejected(settings: Settings, encoder: FakeEncoder) -> None:
    embedder = ChunkEmbedder(settings, model=encoder)

    with pytest.raises(EmbeddingError, match="blank"):
        embedder.embed_query("   ")


def test_model_source_defaults_to_configured_hub_id(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(EMBEDDING_MODEL_PATH_ENV, raising=False)

    assert embedding_model_source(settings) == settings.embedding.model


def test_model_source_uses_local_snapshot_override(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = tmp_path / "all-MiniLM-L6-v2"
    snapshot.mkdir()
    monkeypatch.setenv(EMBEDDING_MODEL_PATH_ENV, str(snapshot))

    assert embedding_model_source(settings) == str(snapshot)


def test_model_source_resolves_relative_override(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = settings.base_dir / "models" / "all-MiniLM-L6-v2"
    snapshot.mkdir(parents=True)
    monkeypatch.setenv(EMBEDDING_MODEL_PATH_ENV, "models/all-MiniLM-L6-v2")

    assert embedding_model_source(settings) == str(snapshot.resolve())


def test_missing_model_override_is_rejected(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(EMBEDDING_MODEL_PATH_ENV, str(tmp_path / "missing"))

    with pytest.raises(EmbeddingError, match="does not exist"):
        embedding_model_source(settings)
