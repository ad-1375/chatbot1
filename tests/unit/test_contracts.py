"""Contract and artifact I/O tests."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from mf_faq.contracts import (
    AnswerResponse,
    Chunk,
    QueryRequest,
    RetrievedEvidence,
    SourcePage,
)
from mf_faq.io_utils import count_jsonl, read_json, read_jsonl, write_json, write_jsonl

NOW = datetime(2026, 11, 8, 10, 0, tzinfo=timezone.utc)


def source_page(**overrides) -> SourcePage:
    values = dict(
        doc_id="c4_hdfc_elss",
        url="https://groww.in/mutual-funds/hdfc-elss-tax-saver-fund-direct-plan-growth",
        title="HDFC ELSS Tax Saver Fund Direct Plan Growth",
        page_type="scheme",
        scheme_id="hdfc_elss",
        fetched_at=NOW,
        http_status=200,
        content_hash="sha256:abc123",
        text="Fund details and expense ratio.",
    )
    values.update(overrides)
    return SourcePage(**values)


def chunk(**overrides) -> Chunk:
    values = dict(
        chunk_id="7f3a9c2d1b4e8a60",
        doc_id="c4_hdfc_elss",
        url="https://groww.in/mutual-funds/hdfc-elss-tax-saver-fund-direct-plan-growth",
        title="HDFC ELSS Tax Saver Fund Direct Plan Growth",
        scheme_id="hdfc_elss",
        section_path=["Fund details", "Expense ratio"],
        field="expense_ratio",
        chunk_type="kv_pair",
        content_class="factual",
        indexed=True,
        exclusion_reason=None,
        text="HDFC ELSS > Fund details > Expense ratio: 0.75%",
        token_count=28,
        content_hash="sha256:def456",
        fetched_at=NOW,
    )
    values.update(overrides)
    return Chunk(**values)


def test_valid_source_page_round_trips() -> None:
    page = source_page()
    restored = SourcePage.model_validate_json(page.model_dump_json())
    assert restored == page


def test_source_page_requires_text() -> None:
    with pytest.raises(ValidationError):
        source_page(text="")


def test_source_page_rejects_relative_url() -> None:
    with pytest.raises(ValidationError, match="absolute"):
        source_page(url="/mutual-funds/hdfc-elss")


def test_chunk_accepts_factual_content() -> None:
    model = chunk()
    assert model.indexed is True
    assert model.section_path == ["Fund details", "Expense ratio"]


def test_excluded_chunk_requires_reason() -> None:
    with pytest.raises(ValidationError, match="exclusion_reason"):
        chunk(indexed=False)

    excluded = chunk(indexed=False, exclusion_reason="performance content")
    assert excluded.exclusion_reason == "performance content"


def test_chunk_type_is_constrained() -> None:
    with pytest.raises(ValidationError):
        chunk(chunk_type="freeform")


def test_answer_status_is_constrained() -> None:
    with pytest.raises(ValidationError):
        AnswerResponse(trace_id="t1", status="maybe")


def test_answer_response_preserves_single_citation() -> None:
    evidence = RetrievedEvidence(
        chunk_id="c1",
        doc_id="c4_hdfc_elss",
        url="https://groww.in/mutual-funds/hdfc-elss-tax-saver-fund-direct-plan-growth",
        title="HDFC ELSS Tax Saver Fund Direct Plan Growth",
        section="Fund details > Expense ratio",
        field="expense_ratio",
        score=0.82,
        text="Expense ratio: 0.75%",
        fetched_at=NOW,
    )
    response = AnswerResponse(
        trace_id="t1",
        status="answered",
        answer="The expense ratio is 0.75%.",
        citation={"url": evidence.url, "title": evidence.title},
        last_updated="2026-11-08",
        evidence=[evidence],
        guardrails={"pii": "pass", "sentences": 1},
        stage_timings_ms={"retrieval": 4.2, "generation": 900.0},
    )

    assert response.citation is not None
    assert response.citation.url == evidence.url
    assert len(response.evidence) == 1


def test_blank_query_rejected() -> None:
    with pytest.raises(ValidationError):
        QueryRequest(query="   ", session_id="s1")

    request = QueryRequest(query="  ELSS lock-in?  ", session_id="s1")
    assert request.query == "ELSS lock-in?"


def test_json_artifact_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "answer.json"
    response = AnswerResponse(trace_id="t1", status="not_found", answer="")
    assert write_json(path, response) == path

    assert read_json(path)["status"] == "not_found"
    assert not (tmp_path / "answer.json.tmp").exists()


def test_jsonl_artifact_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "chunks.jsonl"
    write_jsonl(path, [chunk(), chunk(indexed=False, exclusion_reason="performance")])

    rows = list(read_jsonl(path))
    assert count_jsonl(path) == 2
    assert rows[0]["field"] == "expense_ratio"
    assert rows[1]["indexed"] is False
    assert not (tmp_path / "chunks.jsonl.tmp").exists()
