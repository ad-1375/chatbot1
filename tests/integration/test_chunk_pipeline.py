"""End-to-end chunk pipeline over the five Step 1 fixtures (no network)."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mf_faq.chunking.chunker import TokenCounter, validate_chunks
from mf_faq.chunking.pipeline import ChunkingError, run_chunk
from mf_faq.config import Settings
from mf_faq.contracts import Chunk, SourcePage
from mf_faq.ingest.loader import FetchedPage
from mf_faq.ingest.pipeline import run_ingest
from mf_faq.io_utils import count_jsonl, read_json, read_jsonl

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


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    loaded = Settings.load(CONFIG, base_dir=tmp_path)
    run_ingest(loaded, pages=fixture_pages(loaded))
    return loaded


def test_five_documents_produce_chunks_and_exclusions(settings: Settings) -> None:
    result = run_chunk(settings)

    assert result.ok
    assert result.stats["document_count"] == 5
    assert result.stats["chunk_count"] == len(result.chunks) > 0
    assert result.stats["indexed_count"] > 0
    assert result.stats["excluded_count"] > 0

    assert count_jsonl(result.chunks_path) == len(result.chunks)
    assert count_jsonl(result.excluded_path) == len(result.excluded)
    assert set(result.stats["by_page"]) == set(FIXTURE_FILES)

    for row in read_jsonl(result.chunks_path):
        Chunk.model_validate(row)
    assert all(chunk.indexed for chunk in result.chunks if chunk.content_class == "factual")
    assert all(not chunk.indexed for chunk in result.excluded)
    assert all(chunk.exclusion_reason for chunk in result.excluded)
    assert {
        chunk.content_class for chunk in result.excluded
    } & {"performance", "boilerplate", "navigation"}


def test_stats_and_report_written(settings: Settings) -> None:
    result = run_chunk(settings)
    stats = read_json(result.stats_path)
    report = result.report_path.read_text(encoding="utf-8")

    assert stats["chunk_count"] == len(result.chunks)
    assert stats["indexed_count"] + stats["excluded_count"] == stats["chunk_count"]
    assert stats["config_hash"] == settings.config_hash
    assert stats["max_tokens"] == settings.chunking.max_tokens
    assert set(stats["by_page"]) == set(FIXTURE_FILES)
    assert stats["invariant_violations"] == 0
    assert stats["tokenizer"]["model"] == settings.chunking.tokenizer

    assert "# Chunking report" in report
    assert "Documents: **5/5**" in report
    assert "## Invariants" in report
    assert "| c4_hdfc_elss |" in report
    for doc_id in FIXTURE_FILES:
        assert doc_id in report


def test_no_invariant_violation(settings: Settings) -> None:
    result = run_chunk(settings)
    documents = {
        document.doc_id: document
        for document in (
            SourcePage.model_validate(row)
            for row in read_jsonl(settings.resolved_path("data/processed/documents.jsonl"))
        )
    }
    counter = TokenCounter(settings.chunking.tokenizer)

    assert validate_chunks(
        result.chunks, settings=settings, token_counter=counter
    ) == []
    assert len({chunk.chunk_id for chunk in result.chunks}) == len(result.chunks)

    for chunk in result.chunks:
        source = documents[chunk.doc_id]
        assert chunk.chunk_id
        assert chunk.section_path
        assert chunk.field
        assert chunk.chunk_type in {"kv_pair", "table_row", "list_item", "paragraph"}
        assert chunk.content_hash.startswith("sha256:")
        assert chunk.text.startswith(f"{source.title} >")
        assert chunk.url == source.url
        assert chunk.scheme_id == source.scheme_id
        if chunk.indexed:
            assert chunk.token_count <= settings.chunking.max_tokens
            assert not chunk.oversize
            assert chunk.content_class == "factual"
        else:
            assert chunk.exclusion_reason
        if chunk.oversize:
            assert not chunk.indexed

    counts = Counter(chunk.doc_id for chunk in result.chunks)
    assert dict(counts) == {
        doc_id: result.stats["by_page"][doc_id]["chunks"]
        for doc_id in result.stats["by_page"]
    }


def test_rerun_is_deterministic(settings: Settings) -> None:
    first = run_chunk(settings)
    second = run_chunk(settings)

    assert [(chunk.chunk_id, chunk.text) for chunk in first.chunks] == [
        (chunk.chunk_id, chunk.text) for chunk in second.chunks
    ]
    assert first.stats["by_page"] == second.stats["by_page"]
    assert first.stats["by_field"] == second.stats["by_field"]


def test_missing_step1_artifacts_fail_closed(tmp_path: Path) -> None:
    empty = Settings.load(CONFIG, base_dir=tmp_path / "empty")

    with pytest.raises(ChunkingError, match="run_ingest"):
        run_chunk(empty)

    chunks_path = empty.resolved_path("data/chunks/chunks.jsonl")
    assert not chunks_path.exists()
