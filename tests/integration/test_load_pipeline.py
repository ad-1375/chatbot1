"""End-to-end load pipeline against five HTML fixtures (no network)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from mf_faq.config import Settings
from mf_faq.contracts import SourcePage
from mf_faq.ingest.loader import FetchedPage
from mf_faq.ingest.pipeline import IngestError, run_ingest
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


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings.load(CONFIG, base_dir=tmp_path)


def fixture_pages(settings: Settings) -> list[FetchedPage]:
    seeds = {seed.id: seed for seed in settings.corpus.seed_pages}
    pages: list[FetchedPage] = []
    for doc_id, filename in FIXTURE_FILES.items():
        seed = seeds[doc_id]
        pages.append(
            FetchedPage(
                seed=seed,
                final_url=seed.url,
                status_code=200,
                html=(FIXTURES / filename).read_text(encoding="utf-8"),
                fetched_at=NOW,
                renderer="fixture",
            )
        )
    return pages


def test_five_fixtures_produce_five_valid_source_pages(settings: Settings) -> None:
    result = run_ingest(settings, pages=fixture_pages(settings))

    assert result.ok
    assert len(result.documents) == 5
    assert len(result.failures) == 0

    documents_path = settings.resolved_path("data/processed/documents.jsonl")
    assert count_jsonl(documents_path) == 5

    rows = list(read_jsonl(documents_path))
    assert [row["doc_id"] for row in rows] == [
        "c1_amc_hdfc",
        "c2_hdfc_large_cap",
        "c3_hdfc_flexi_cap",
        "c4_hdfc_elss",
        "c5_hdfc_mid_cap",
    ]
    for row in rows:
        page = SourcePage.model_validate(row)
        assert page.text
        assert page.content_hash.startswith("sha256:")
        assert page.http_status == 200


def test_blocks_artifact_is_written_for_every_page(settings: Settings) -> None:
    result = run_ingest(settings, pages=fixture_pages(settings))

    rows = list(read_jsonl(result.blocks_path))
    assert len(rows) == 5
    assert {row["doc_id"] for row in rows} == set(FIXTURE_FILES)
    assert all(row["blocks"] for row in rows)
    assert any(
        block["kind"] == "kv_pair"
        for row in rows
        for block in row["blocks"]
    )


def test_load_report_lists_pages_and_coverage(settings: Settings) -> None:
    result = run_ingest(settings, pages=fixture_pages(settings))
    report = result.report_path.read_text(encoding="utf-8")

    assert "Pages loaded: **5/5**" in report
    assert "Fact coverage" in report
    assert "Expense ratio" in report
    for doc_id in FIXTURE_FILES:
        assert doc_id in report
    # The ELSS fixture contains the lock-in fact; Large Cap does not.
    assert "| c4_hdfc_elss | yes |" in report
    assert "| c2_hdfc_large_cap |" in report


def test_manifest_matches_documents_and_raw_snapshots(settings: Settings) -> None:
    result = run_ingest(settings, pages=fixture_pages(settings))
    manifest = read_json(result.manifest_path)

    assert manifest["document_count"] == 5
    assert manifest["failure_count"] == 0
    assert manifest["config_hash"] == settings.config_hash
    assert len(manifest["documents"]) == 5
    assert set(manifest["coverage"]) == set(FIXTURE_FILES)
    assert manifest["coverage"]["c4_hdfc_elss"]["lock_in"] is True
    assert manifest["coverage"]["c2_hdfc_large_cap"]["lock_in"] is False

    for document in manifest["documents"]:
        raw_path = settings.base_dir / document["raw_path"]
        assert raw_path.is_file()
        assert raw_path.stat().st_size > 0


def test_incomplete_batch_fails_without_writing_documents(settings: Settings) -> None:
    with pytest.raises(IngestError, match="4/5"):
        run_ingest(settings, pages=fixture_pages(settings)[:4])

    documents_path = settings.resolved_path("data/processed/documents.jsonl")
    assert not documents_path.exists()
