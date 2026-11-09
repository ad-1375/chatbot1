"""Security: PII is blocked before retrieval and never persisted."""

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
from mf_faq.index.chroma_store import ChromaStore

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "config" / "config.yaml"
ELSS_URL = "https://groww.in/mutual-funds/hdfc-elss-tax-saver-fund-direct-plan-growth"
ELSS_TITLE = "HDFC ELSS Tax Saver Fund Direct Plan Growth"
NOW = datetime(2026, 11, 9, 10, 0, tzinfo=timezone.utc)


class ExplodingEmbedder:
    def embed_query(self, query: str) -> np.ndarray:
        raise AssertionError("PII reached the embedder")


class ExplodingLLM:
    provider = "fake"
    model = "fake-model"

    def complete(self, *, system: str, user: str):
        raise AssertionError("PII reached the LLM")


def elss_chunk() -> Chunk:
    text = f"{ELSS_TITLE} > Expense ratio: 1.20%"
    return Chunk(
        chunk_id="elss_expense_ratio",
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
        text=text,
        token_count=12,
        content_hash=f"sha256:{hashlib.sha256(text.encode()).hexdigest()}",
        fetched_at=NOW,
    )


@pytest.fixture()
def settings() -> Settings:
    return Settings.load(CONFIG)


@pytest.fixture()
def store(settings: Settings) -> ChromaStore:
    created = ChromaStore(settings, client=chromadb.EphemeralClient())
    created.drop()
    created.open()
    vector = np.zeros(EXPECTED_DIMENSION, dtype=np.float32)
    vector[0] = 1.0
    created.upsert([elss_chunk()], np.asarray([vector], dtype=np.float32))
    return created


def test_pii_block_leaves_no_raw_value_in_any_trace(
    settings: Settings, store: ChromaStore, tmp_path: Path
) -> None:
    trace_root = tmp_path / "traces"
    service = AnswerService(
        settings,
        store=store,
        embedder=ExplodingEmbedder(),
        llm=ExplodingLLM(),
        trace_root=trace_root,
    )
    raw = "My PAN is ABCDE1234F and my OTP is 123456"

    response = service.answer(QueryRequest(query=raw, session_id="security-session"))

    assert response.status == "blocked_pii"
    assert response.citation is None
    assert response.evidence == []

    trace_path = trace_root / "answers" / f"{response.trace_id}.json"
    assert trace_path.is_file()
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert trace["query"] is None
    assert not (trace_root / "prompts" / f"{response.trace_id}.txt").exists()
    assert not (trace_root / "retrieval" / f"{response.trace_id}.json").exists()

    for value in ("ABCDE1234F", "123456"):
        for path in trace_root.rglob("*"):
            if path.is_file():
                assert value not in path.read_text(encoding="utf-8"), path


def test_security_log_has_types_and_hash_only(tmp_path: Path) -> None:
    from mf_faq.guardrails.pii import log_pii_block, screen_pii

    raw = "test@example.com"
    result = screen_pii(raw)
    path = log_pii_block(
        result,
        trace_id="trace",
        session_id="session",
        log_path=tmp_path / "security" / "pii_blocks.jsonl",
    )

    text = path.read_text(encoding="utf-8")
    payload = json.loads(text)
    assert payload["pii_types"] == ["email"]
    assert set(payload) == {
        "timestamp",
        "trace_id",
        "session_id",
        "pii_types",
        "query_hash",
    }
    assert raw not in text
