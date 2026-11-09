"""End-to-end indexing over the five Step 1 fixtures (no network).

Ingest → chunk → embed → Chroma all run locally. Chroma uses an ephemeral
client so the test leaves no files behind; the live run uses ``data/chroma``.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

import chromadb
import numpy as np
import pytest

from mf_faq.chunking.pipeline import run_chunk
from mf_faq.config import Settings
from mf_faq.contracts import Chunk, IndexManifest
from mf_faq.embeddings.embedder import EXPECTED_DIMENSION
from mf_faq.embeddings.pipeline import run_embed
from mf_faq.index.chroma_store import ChromaStore, IndexingError
from mf_faq.index.pipeline import run_index
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


def make_chunk(chunk_id: str) -> Chunk:
    text = f"{chunk_id}: stray fact"
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
        indexed=True,
        text=text,
        token_count=5,
        content_hash=f"sha256:{hashlib.sha256(text.encode()).hexdigest()}",
        fetched_at=NOW,
    )


def make_vectors(count: int) -> np.ndarray:
    rng = np.random.default_rng(11)
    vectors = rng.standard_normal((count, EXPECTED_DIMENSION)).astype(np.float32)
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    return vectors


@pytest.fixture(scope="module")
def settings(tmp_path_factory: pytest.TempPathFactory) -> Settings:
    loaded = Settings.load(CONFIG, base_dir=tmp_path_factory.mktemp("index"))
    run_ingest(loaded, pages=fixture_pages(loaded))
    run_chunk(loaded)
    run_embed(loaded)
    return loaded


@pytest.fixture()
def store(settings: Settings) -> ChromaStore:
    created = ChromaStore(settings, client=chromadb.EphemeralClient())
    created.drop()  # ephemeral clients share one in-process store
    return created


def indexed_chunks(settings: Settings) -> list[Chunk]:
    rows = list(read_jsonl(settings.resolved_path("data/chunks/chunks.jsonl")))
    return [Chunk.model_validate(row) for row in rows if row.get("indexed") is True]


def test_index_built_from_fixture_embeddings_has_expected_count(
    settings: Settings, store: ChromaStore
) -> None:
    expected = indexed_chunks(settings)
    result = run_index(settings, store=store)

    assert result.ok
    assert result.count == len(expected) > 0
    assert store.count() == len(expected)
    assert set(store.all_ids()) == {chunk.chunk_id for chunk in expected}
    assert all(test["ok"] for test in result.stats["filters"])
    assert {test["filter"] for test in result.stats["filters"]} == {
        "scheme_id",
        "field",
        "indexed",
    }

    sample = expected[0]
    record = store.collection.get(ids=[sample.chunk_id])
    assert record["documents"] == [sample.text]
    assert record["metadatas"][0]["field"] == sample.field
    assert record["metadatas"][0]["indexed"] is True


def test_index_manifest_validates(settings: Settings, store: ChromaStore) -> None:
    result = run_index(settings, store=store)
    manifest = IndexManifest.model_validate(read_json(result.manifest_path))
    report = result.report_path.read_text(encoding="utf-8")

    assert manifest.collection == settings.vector_db.collection
    assert manifest.count == result.count == len(result.chunks)
    assert manifest.model == settings.embedding.model
    assert manifest.dimension == EXPECTED_DIMENSION == 384
    assert manifest.distance == settings.vector_db.distance
    assert manifest.config_hash == settings.config_hash
    assert result.stats["config_hash"] == settings.config_hash

    assert "# Index report" in report
    assert f"Collection: `{settings.vector_db.collection}`" in report
    assert f"Count: **{result.count}**" in report
    assert "## Filter smoke tests" in report
    for test in result.stats["filters"]:
        assert test["filter"] in report


def test_repeat_index_is_idempotent(settings: Settings, store: ChromaStore) -> None:
    first = run_index(settings, store=store)
    first_ids = set(store.all_ids())
    second = run_index(settings, store=store)

    assert store.count() == first.count == second.count
    assert set(store.all_ids()) == first_ids
    assert second.stats["upserted"] == len(second.chunks)
    assert second.stats["rebuild"] is False


def test_rebuild_drops_and_recreates_the_collection(
    settings: Settings, store: ChromaStore
) -> None:
    first = run_index(settings, store=store)
    stray = make_chunk("stray_record")
    store.upsert([stray], make_vectors(1))
    assert store.count() == first.count + 1

    rebuilt = run_index(settings, store=store, rebuild=True)

    assert rebuilt.stats["rebuild"] is True
    assert store.count() == rebuilt.count == first.count
    assert "stray_record" not in set(store.all_ids())
    assert rebuilt.manifest.count == first.count


def test_stale_records_require_rebuild(settings: Settings, store: ChromaStore) -> None:
    run_index(settings, store=store)
    store.upsert([make_chunk("stray_record")], make_vectors(1))

    with pytest.raises(IndexingError, match="--rebuild"):
        run_index(settings, store=store)


def test_missing_step3_artifacts_fail_closed(tmp_path: Path) -> None:
    empty = Settings.load(CONFIG, base_dir=tmp_path / "empty")

    with pytest.raises(IndexingError, match="run_chunk"):
        run_index(empty)

    assert not empty.resolved_path("data/index_manifest.json").exists()
