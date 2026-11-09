"""End-to-end embedding pipeline over the five Step 1 fixtures (no network).

The chunk and embed steps use the real local MiniLM model from the Hugging Face
cache. The fixture pages are ingested from ``tests/fixtures/html`` so the run
never touches Groww.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest

from mf_faq.chunking.pipeline import run_chunk
from mf_faq.config import Settings
from mf_faq.contracts import Chunk, EmbeddingManifest
from mf_faq.embeddings.embedder import EXPECTED_DIMENSION, load_encoder
from mf_faq.embeddings.pipeline import EmbeddingError, run_embed
from mf_faq.ingest.loader import FetchedPage
from mf_faq.ingest.pipeline import run_ingest
from mf_faq.io_utils import read_json, read_jsonl

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "config" / "config.yaml"
FIXTURES = ROOT / "tests" / "fixtures" / "html"
NOW = datetime(2026, 11, 8, 10, 0, tzinfo=timezone.utc)

FIXTURE_FILES = {
    "c1_amc_hdfc": "c1_amc_hdfc.html",
    "c2_hdfc_large_cap": "c2_hdfc_large_cap.html",
    "c3_hdfc_flexi_cap": "c3_hdfc_flexi_cap.html",
    "c4_hdfc_elss": "c4_hdfc_elss.html",
    "c5_hdfc_mid_cap": "c5_hdfc_mid_cap.html",
}


def fixture_pages(settings: Settings) -> list[FetchedPage]:
    seeds = {seed.id: seed for seed in settings.corpus.seed_pages}
    return [
        FetchedPage(
            seed=seeds[doc_id],
            final_url=seeds[doc_id].url,
            status_code=200,
            html=(FIXTURES / filename).read_text(encoding="utf-8"),
            fetched_at=NOW,
            renderer="fixture",
        )
        for doc_id, filename in FIXTURE_FILES.items()
    ]


@pytest.fixture(scope="module")
def settings(tmp_path_factory: pytest.TempPathFactory) -> Settings:
    loaded = Settings.load(CONFIG, base_dir=tmp_path_factory.mktemp("embed"))
    run_ingest(loaded, pages=fixture_pages(loaded))
    run_chunk(loaded)
    return loaded


@pytest.fixture(scope="module")
def encoder(settings: Settings):
    return load_encoder(settings)


def indexed_chunks(settings: Settings) -> list[Chunk]:
    rows = list(read_jsonl(settings.resolved_path("data/chunks/chunks.jsonl")))
    return [Chunk.model_validate(row) for row in rows if row.get("indexed") is True]


def test_vector_count_and_order_match_indexed_chunks(
    settings: Settings, encoder
) -> None:
    expected = indexed_chunks(settings)
    result = run_embed(settings, encoder=encoder)

    assert result.ok
    assert result.count == len(expected) > 0
    assert result.vectors.shape == (len(expected), EXPECTED_DIMENSION)
    assert result.vectors.dtype == np.float32
    assert result.ids == [chunk.chunk_id for chunk in expected]

    assert read_json(result.ids_path) == result.ids
    stored = np.load(result.embeddings_path, allow_pickle=False)
    assert stored.shape == (len(expected), EXPECTED_DIMENSION)
    assert stored.dtype == np.float32
    np.testing.assert_array_equal(stored, result.vectors)

    assert np.isfinite(result.vectors).all()
    norms = np.linalg.norm(result.vectors, axis=1)
    np.testing.assert_allclose(norms, np.ones(len(expected)), atol=1e-3)
    assert len(set(result.ids)) == len(result.ids)


def test_manifest_matches_active_config(settings: Settings, encoder) -> None:
    result = run_embed(settings, encoder=encoder)
    manifest = EmbeddingManifest.model_validate(read_json(result.manifest_path))

    assert manifest.model == settings.embedding.model
    assert manifest.dimension == EXPECTED_DIMENSION == 384
    assert manifest.max_seq_length == settings.embedding.max_seq_length
    assert manifest.normalized is settings.embedding.normalize
    assert manifest.config_hash == settings.config_hash
    assert manifest.count == len(result.ids) == result.stats["count"]
    assert result.stats["config_hash"] == settings.config_hash


def test_preview_and_report_written(settings: Settings, encoder) -> None:
    result = run_embed(settings, encoder=encoder)
    preview = read_json(result.preview_path)
    report = result.report_path.read_text(encoding="utf-8")

    assert preview["model"] == settings.embedding.model
    assert preview["dimension"] == EXPECTED_DIMENSION
    assert preview["count"] == len(result.ids)
    assert len(preview["preview"]) == min(3, len(result.ids))

    first = preview["preview"][0]
    assert first["chunk_id"] == result.ids[0]
    assert len(first["first_eight_dimensions"]) == 8
    assert abs(first["norm"] - 1.0) <= 1e-3
    assert first["text_preview"]

    assert "# Embedding report" in report
    assert settings.embedding.model in report
    assert f"Indexed chunks: **{len(result.ids)}**" in report
    assert "## Invariants" in report


def test_rerun_is_deterministic(settings: Settings, encoder) -> None:
    first = run_embed(settings, encoder=encoder)
    second = run_embed(settings, encoder=encoder)

    assert first.ids == second.ids
    assert read_json(first.ids_path) == read_json(second.ids_path)
    np.testing.assert_allclose(first.vectors, second.vectors, rtol=0, atol=1e-6)
    assert (
        read_json(first.manifest_path)["config_hash"]
        == read_json(second.manifest_path)["config_hash"]
    )


def test_missing_step2_artifacts_fail_closed(tmp_path: Path) -> None:
    empty = Settings.load(CONFIG, base_dir=tmp_path / "empty")

    with pytest.raises(EmbeddingError, match="run_chunk"):
        run_embed(empty)

    assert not empty.resolved_path("data/embeddings/embeddings.npy").exists()
    assert not empty.resolved_path("data/embeddings/embedding_ids.json").exists()
