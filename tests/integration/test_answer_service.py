"""End-to-end answer service tests with an ephemeral index and fake LLM."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import chromadb
import numpy as np
import pytest

from mf_faq.config import Settings
from mf_faq.contracts import Chunk, QueryRequest
from mf_faq.embeddings.embedder import EXPECTED_DIMENSION
from mf_faq.generation.answer import AnswerService
from mf_faq.generation.client import LLMCompletion, LLMError
from mf_faq.guardrails.sentences import count_sentences
from mf_faq.index.chroma_store import ChromaStore

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "config" / "config.yaml"
ELSS_URL = "https://groww.in/mutual-funds/hdfc-elss-tax-saver-fund-direct-plan-growth"
AMC_URL = "https://groww.in/mutual-funds/amc/hdfc-mutual-funds"
CORPUS_URLS = {
    AMC_URL,
    "https://groww.in/mutual-funds/hdfc-large-cap-fund-direct-growth",
    "https://groww.in/mutual-funds/hdfc-equity-fund-direct-growth",
    ELSS_URL,
    "https://groww.in/mutual-funds/hdfc-mid-cap-fund-direct-growth",
}
ELSS_TITLE = "HDFC ELSS Tax Saver Fund Direct Plan Growth"
NOW = datetime(2026, 11, 9, 10, 0, tzinfo=timezone.utc)
CHUNK_ID = "elss_expense_ratio"
CHUNK_TEXT = f"{ELSS_TITLE} > Expense ratio: 1.20%"


def basis(index: int) -> np.ndarray:
    vector = np.zeros(EXPECTED_DIMENSION, dtype=np.float32)
    vector[index] = 1.0
    return vector


def make_elss_chunk() -> Chunk:
    return Chunk(
        chunk_id=CHUNK_ID,
        doc_id="c4_hdfc_elss",
        url=ELSS_URL,
        title=ELSS_TITLE,
        scheme_id="hdfc_elss",
        page_type="scheme",
        section_path=["Fund details", "Expense ratio"],
        field="expense_ratio",
        chunk_type="kv_pair",
        content_class="factual",
        indexed=True,
        exclusion_reason=None,
        text=CHUNK_TEXT,
        token_count=12,
        content_hash=f"sha256:{hashlib.sha256(CHUNK_TEXT.encode()).hexdigest()}",
        fetched_at=NOW,
    )


def valid_answer(answer: str = "The expense ratio is 1.20%.", url: str = ELSS_URL) -> str:
    return json.dumps(
        {
            "status": "answered",
            "answer": answer,
            "citation_url": url,
            "citation_title": "Ignored model title",
            "section": "Fund details",
            "field": "expense_ratio",
            "confidence": 0.9,
        }
    )


class FakeLLM:
    provider = "fake"
    model = "fake-model"

    def __init__(self, outputs: list[str] | None = None) -> None:
        self.outputs = list(outputs or [])
        self.calls = 0

    def complete(self, *, system: str, user: str) -> LLMCompletion:
        self.calls += 1
        if not self.outputs:
            raise AssertionError("LLM was called but no fake output was queued")
        return LLMCompletion(
            text=self.outputs.pop(0),
            provider=self.provider,
            model=self.model,
        )


class FakeEmbedder:
    def __init__(self, vector: np.ndarray | None = None, *, explode: bool = False) -> None:
        self.vector = vector if vector is not None else basis(0)
        self.explode = explode
        self.calls = 0

    def embed_query(self, query: str) -> np.ndarray:
        self.calls += 1
        if self.explode:
            raise AssertionError("embedding ran when it should have short-circuited")
        return self.vector


@pytest.fixture()
def settings() -> Settings:
    return Settings.load(CONFIG)


@pytest.fixture()
def store(settings: Settings) -> ChromaStore:
    created = ChromaStore(settings, client=chromadb.EphemeralClient())
    created.drop()
    created.open()
    return created


def add_elss_chunk(store: ChromaStore) -> None:
    store.upsert([make_elss_chunk()], np.asarray([basis(0)], dtype=np.float32))


def service(
    settings: Settings,
    store: ChromaStore,
    *,
    llm: FakeLLM,
    embedder: FakeEmbedder,
    trace_root: Path,
) -> AnswerService:
    return AnswerService(
        settings,
        store=store,
        embedder=embedder,
        llm=llm,
        trace_root=trace_root,
    )


def request(query: str, **kwargs) -> QueryRequest:
    return QueryRequest(query=query, session_id="test-session", **kwargs)


def trace_json(trace_root: Path, kind: str, trace_id: str) -> dict:
    path = trace_root / kind / f"{trace_id}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_happy_path_answered_with_one_evidence_citation(
    settings: Settings, store: ChromaStore, tmp_path: Path
) -> None:
    add_elss_chunk(store)
    llm = FakeLLM([valid_answer(url="https://evil.example/invented")])
    embedder = FakeEmbedder(basis(0))
    answer_service = service(
        settings,
        store,
        llm=llm,
        embedder=embedder,
        trace_root=tmp_path / "traces",
    )

    response = answer_service.answer(
        request("What is the expense ratio of HDFC ELSS Tax Saver?")
    )

    assert response.status == "answered"
    assert count_sentences(response.answer) <= 3
    assert response.citation is not None
    assert response.citation.url == ELSS_URL
    assert response.last_updated == "2026-11-09"
    assert len(response.evidence) == 1
    assert response.evidence[0].chunk_id == CHUNK_ID
    assert {item.doc_id for item in response.evidence} == {"c4_hdfc_elss"}
    assert llm.calls == 1

    answer_trace = trace_json(tmp_path / "traces", "answers", response.trace_id)
    assert answer_trace["status"] == "answered"
    assert answer_trace["citation_url"] == ELSS_URL
    assert answer_trace["evidence_ids"] == [CHUNK_ID]
    assert answer_trace["fallback"] is False
    assert answer_trace["repair_attempted"] is False

    retrieval_trace = trace_json(
        tmp_path / "traces", "retrieval", response.trace_id
    )
    assert retrieval_trace["resolved_scheme"] == "hdfc_elss"
    assert retrieval_trace["resolved_field"] == "expense_ratio"
    assert retrieval_trace["selected"] == [CHUNK_ID]
    assert retrieval_trace["filter_fallback"] is False

    prompt_path = tmp_path / "traces" / "prompts" / f"{response.trace_id}.txt"
    assert prompt_path.is_file()
    prompt_text = prompt_path.read_text(encoding="utf-8")
    assert "CONTEXT" in prompt_text
    assert ELSS_URL in prompt_text
    assert "evil.example" not in prompt_text

    raw_path = tmp_path / "traces" / "responses" / f"{response.trace_id}.txt"
    assert raw_path.is_file()
    assert '"status": "answered"' in raw_path.read_text(encoding="utf-8")


def test_invalid_json_repairs_once_then_uses_extractive_fallback(
    settings: Settings, store: ChromaStore, tmp_path: Path
) -> None:
    add_elss_chunk(store)
    llm = FakeLLM(["this is not json", "this is still not json"])
    answer_service = service(
        settings,
        store,
        llm=llm,
        embedder=FakeEmbedder(basis(0)),
        trace_root=tmp_path / "traces",
    )

    response = answer_service.answer(
        request("What is the expense ratio of HDFC ELSS Tax Saver?")
    )

    assert response.status == "answered"
    assert response.guardrails["fallback"] is True
    assert "1.20" in response.answer
    assert response.citation is not None
    assert response.citation.url == ELSS_URL
    assert llm.calls == 2

    answer_trace = trace_json(tmp_path / "traces", "answers", response.trace_id)
    assert answer_trace["fallback"] is True
    assert answer_trace["repair_attempted"] is True
    prompt_text = (
        tmp_path / "traces" / "prompts" / f"{response.trace_id}.txt"
    ).read_text(encoding="utf-8")
    assert "--- REPAIR ---" in prompt_text

    raw_text = (
        tmp_path / "traces" / "responses" / f"{response.trace_id}.txt"
    ).read_text(encoding="utf-8")
    assert "RAW RESPONSE" in raw_text
    assert "REPAIR RESPONSE" in raw_text


def test_invalid_json_then_valid_repair_avoids_fallback(
    settings: Settings, store: ChromaStore, tmp_path: Path
) -> None:
    add_elss_chunk(store)
    repaired = valid_answer(
        "The expense ratio of HDFC ELSS Tax Saver Fund Direct Plan Growth is 1.20%."
    )
    llm = FakeLLM(["not json", repaired])
    answer_service = service(
        settings,
        store,
        llm=llm,
        embedder=FakeEmbedder(basis(0)),
        trace_root=tmp_path / "traces",
    )

    response = answer_service.answer(
        request("What is the expense ratio of HDFC ELSS Tax Saver?")
    )

    assert response.status == "answered"
    assert response.guardrails["fallback"] is False
    assert response.guardrails["repair_attempted"] is True
    assert "1.20" in response.answer
    assert llm.calls == 2


def test_advice_performance_out_of_scope_and_clarify_skip_llm(
    settings: Settings, store: ChromaStore, tmp_path: Path
) -> None:
    llm = FakeLLM([])
    embedder = FakeEmbedder(explode=True)
    answer_service = service(
        settings,
        store,
        llm=llm,
        embedder=embedder,
        trace_root=tmp_path / "traces",
    )

    cases = [
        ("Should I buy HDFC ELSS?", "refused_advice", True),
        ("What is the 5-year return of HDFC Mid Cap?", "refused_performance", True),
        ("Tell me about SBI Bluechip Fund", "out_of_scope", False),
        ("What is the minimum SIP?", "clarify", False),
    ]

    for query, expected_status, needs_citation in cases:
        response = answer_service.answer(request(query))
        assert response.status == expected_status, query
        assert (response.citation is not None) is needs_citation, query

        answer_trace = trace_json(tmp_path / "traces", "answers", response.trace_id)
        assert answer_trace["status"] == expected_status
        assert answer_trace["query"] == query
        assert not (
            tmp_path / "traces" / "prompts" / f"{response.trace_id}.txt"
        ).exists()
        assert not (
            tmp_path / "traces" / "retrieval" / f"{response.trace_id}.json"
        ).exists()

    assert llm.calls == 0
    assert embedder.calls == 0


def test_pii_block_short_circuits_and_never_persists_raw_text(
    settings: Settings, store: ChromaStore, tmp_path: Path
) -> None:
    llm = FakeLLM([])
    embedder = FakeEmbedder(explode=True)
    trace_root = tmp_path / "traces"
    answer_service = service(
        settings,
        store,
        llm=llm,
        embedder=embedder,
        trace_root=trace_root,
    )
    raw = "My PAN is ABCDE1234F"

    response = answer_service.answer(request(raw))

    assert response.status == "blocked_pii"
    assert response.citation is None
    assert llm.calls == 0
    assert embedder.calls == 0

    answer_trace = trace_json(trace_root, "answers", response.trace_id)
    assert answer_trace["status"] == "blocked_pii"
    assert answer_trace["query"] is None
    assert not (trace_root / "prompts" / f"{response.trace_id}.txt").exists()
    assert not (trace_root / "retrieval" / f"{response.trace_id}.json").exists()

    security_lines = (
        trace_root / "security" / "pii_blocks.jsonl"
    ).read_text(encoding="utf-8").splitlines()
    assert len(security_lines) == 1
    security = json.loads(security_lines[0])
    assert security["pii_types"] == ["pan"]
    assert security["query_hash"].startswith("sha256:")

    for path in trace_root.rglob("*"):
        if path.is_file():
            assert "ABCDE1234F" not in path.read_text(encoding="utf-8"), path


def test_no_evidence_returns_not_found_without_calling_the_llm(
    settings: Settings, store: ChromaStore, tmp_path: Path
) -> None:
    add_elss_chunk(store)
    llm = FakeLLM([])
    trace_root = tmp_path / "traces"
    answer_service = service(
        settings,
        store,
        llm=llm,
        embedder=FakeEmbedder(basis(5)),
        trace_root=trace_root,
    )

    response = answer_service.answer(request("benchmark of HDFC ELSS"))

    assert response.status == "not_found"
    assert response.citation is not None
    assert response.citation.url == ELSS_URL
    assert response.answer
    assert llm.calls == 0
    assert not (trace_root / "prompts" / f"{response.trace_id}.txt").exists()

    retrieval_trace = trace_json(trace_root, "retrieval", response.trace_id)
    assert retrieval_trace["selected"] == []
    assert retrieval_trace["filter_fallback"] is True


def test_pii_echo_falls_back_without_writing_raw_pii(
    settings: Settings, store: ChromaStore, tmp_path: Path
) -> None:
    add_elss_chunk(store)
    llm = FakeLLM(
        [
            valid_answer(
                answer="Your PAN ABCDE1234F is on file.",
            )
        ]
    )
    trace_root = tmp_path / "traces"
    answer_service = service(
        settings,
        store,
        llm=llm,
        embedder=FakeEmbedder(basis(0)),
        trace_root=trace_root,
    )

    response = answer_service.answer(
        request("What is the expense ratio of HDFC ELSS Tax Saver?")
    )

    assert response.status == "answered"
    assert response.guardrails["fallback"] is True
    assert "ABCDE1234F" not in response.answer
    assert llm.calls == 1

    answer_trace = trace_json(trace_root, "answers", response.trace_id)
    assert answer_trace["fallback"] is True
    assert answer_trace["repair_attempted"] is False
    prompt_text = (
        trace_root / "prompts" / f"{response.trace_id}.txt"
    ).read_text(encoding="utf-8")
    assert "ABCDE1234F" not in prompt_text


def test_model_not_found_status_uses_closest_page(
    settings: Settings, store: ChromaStore, tmp_path: Path
) -> None:
    add_elss_chunk(store)
    llm = FakeLLM([json.dumps({"status": "not_found"})])
    answer_service = service(
        settings,
        store,
        llm=llm,
        embedder=FakeEmbedder(basis(0)),
        trace_root=tmp_path / "traces",
    )

    response = answer_service.answer(
        request("What is the expense ratio of HDFC ELSS Tax Saver?")
    )

    assert response.status == "not_found"
    assert response.citation is not None
    assert response.citation.url == ELSS_URL


def test_too_long_query_is_a_safe_error_without_llm(
    settings: Settings, store: ChromaStore, tmp_path: Path
) -> None:
    llm = FakeLLM([])
    answer_service = service(
        settings,
        store,
        llm=llm,
        embedder=FakeEmbedder(explode=True),
        trace_root=tmp_path / "traces",
    )

    response = answer_service.answer(request("x" * 501))

    assert response.status == "error"
    assert "shorten" in response.answer.lower()
    assert response.citation is not None
    assert llm.calls == 0


def test_statement_download_absence_returns_not_found_with_closest_page(
    settings: Settings, store: ChromaStore, tmp_path: Path
) -> None:
    # G25: the fact is absent, so no sixth page is fetched and the AMC page is cited.
    llm = FakeLLM([])
    answer_service = service(
        settings,
        store,
        llm=llm,
        embedder=FakeEmbedder(basis(5)),
        trace_root=tmp_path / "traces",
    )

    response = answer_service.answer(request("capital gains statement download"))

    assert response.status == "not_found"
    assert response.citation is not None
    assert response.citation.url == AMC_URL
    assert response.citation.url in CORPUS_URLS
    assert llm.calls == 0


def test_unknown_field_without_evidence_returns_not_found(
    settings: Settings, store: ChromaStore, tmp_path: Path
) -> None:
    # G26: a resolved field with no matching evidence is not_found.
    llm = FakeLLM([])
    answer_service = service(
        settings,
        store,
        llm=llm,
        embedder=FakeEmbedder(basis(5)),
        trace_root=tmp_path / "traces",
    )

    response = answer_service.answer(
        request("What is the fund manager of HDFC ELSS Tax Saver?")
    )

    assert response.status == "not_found"
    assert response.citation is not None
    assert response.citation.url == ELSS_URL
    assert llm.calls == 0


def test_two_model_links_are_stripped_and_only_evidence_is_cited(
    settings: Settings, store: ChromaStore, tmp_path: Path
) -> None:
    # G37: only the validated evidence link survives.
    add_elss_chunk(store)
    two_links = (
        "See [one](https://evil.example/one) and "
        "[two](https://evil.example/two). The expense ratio is 1.20%."
    )
    llm = FakeLLM([valid_answer(answer=two_links)])
    answer_service = service(
        settings,
        store,
        llm=llm,
        embedder=FakeEmbedder(basis(0)),
        trace_root=tmp_path / "traces",
    )

    response = answer_service.answer(
        request("What is the expense ratio of HDFC ELSS Tax Saver?")
    )

    assert response.status == "answered"
    assert response.citation is not None
    assert response.citation.url == ELSS_URL
    assert "http" not in response.answer
    assert "evil.example" not in response.model_dump_json()
    assert count_sentences(response.answer) <= 3


def test_ungrounded_number_uses_extractive_fallback(
    settings: Settings, store: ChromaStore, tmp_path: Path
) -> None:
    # G38: a number absent from the cited chunk is rejected, then replaced.
    add_elss_chunk(store)
    llm = FakeLLM(
        [
            valid_answer(answer="The expense ratio is 0.99%."),
            "not json",
        ]
    )
    answer_service = service(
        settings,
        store,
        llm=llm,
        embedder=FakeEmbedder(basis(0)),
        trace_root=tmp_path / "traces",
    )

    response = answer_service.answer(
        request("What is the expense ratio of HDFC ELSS Tax Saver?")
    )

    assert response.status == "answered"
    assert response.guardrails["fallback"] is True
    assert "1.20" in response.answer
    assert "0.99" not in response.answer
    assert llm.calls == 2


def test_empty_model_answer_uses_extractive_fallback(
    settings: Settings, store: ChromaStore, tmp_path: Path
) -> None:
    # G41: an empty model answer falls back to the top chunk.
    add_elss_chunk(store)
    llm = FakeLLM([valid_answer(answer=""), "not json"])
    answer_service = service(
        settings,
        store,
        llm=llm,
        embedder=FakeEmbedder(basis(0)),
        trace_root=tmp_path / "traces",
    )

    response = answer_service.answer(
        request("What is the expense ratio of HDFC ELSS Tax Saver?")
    )

    assert response.status == "answered"
    assert response.guardrails["fallback"] is True
    assert "1.20" in response.answer
    assert response.citation is not None
    assert response.citation.url == ELSS_URL
    assert llm.calls == 2


class FailingLLM:
    provider = "fake"
    model = "fake-model"

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, *, system: str, user: str):
        self.calls += 1
        raise LLMError("provider exploded; raw payload SECRET")


def test_provider_failure_returns_safe_error_with_closest_page(
    settings: Settings, store: ChromaStore, tmp_path: Path
) -> None:
    # G43: a provider failure surfaces as a safe error, never raw payload.
    add_elss_chunk(store)
    llm = FailingLLM()
    trace_root = tmp_path / "traces"
    answer_service = service(
        settings,
        store,
        llm=llm,
        embedder=FakeEmbedder(basis(0)),
        trace_root=trace_root,
    )

    response = answer_service.answer(
        request("What is the expense ratio of HDFC ELSS Tax Saver?")
    )

    assert response.status == "error"
    assert response.citation is not None
    assert response.citation.url == ELSS_URL
    assert "SECRET" not in response.answer
    assert llm.calls == 1

    answer_trace = trace_json(trace_root, "answers", response.trace_id)
    assert answer_trace["status"] == "error"
    assert "SECRET" not in json.dumps(answer_trace)


def test_raw_response_with_pii_is_never_written(
    settings: Settings, store: ChromaStore, tmp_path: Path
) -> None:
    add_elss_chunk(store)
    llm = FakeLLM([valid_answer("The PAN is ABCDE1234F.", url=ELSS_URL)])
    trace_root = tmp_path / "traces"
    answer_service = service(
        settings,
        store,
        llm=llm,
        embedder=FakeEmbedder(basis(0)),
        trace_root=trace_root,
    )

    response = answer_service.answer(
        request("What is the expense ratio of HDFC ELSS Tax Saver?")
    )

    raw_path = trace_root / "responses" / f"{response.trace_id}.txt"
    assert not raw_path.exists()
    assert "ABCDE1234F" not in json.dumps(
        trace_json(trace_root, "answers", response.trace_id)
    )

