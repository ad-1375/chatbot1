"""Shared helpers for Streamlit ``AppTest`` UI tests.

The app accepts injected ``settings``, ``answer_service``, and ``chroma_store``
objects through ``st.session_state``. That keeps UI tests hermetic: no provider
key, no live index, and no network calls.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np
from streamlit.testing.v1 import AppTest

from mf_faq.config import Settings
from mf_faq.contracts import AnswerResponse, Citation, RetrievedEvidence
from mf_faq.io_utils import write_json, write_jsonl, write_text

PROJECT = Path(__file__).resolve().parents[1]
CONFIG = PROJECT / "config" / "config.yaml"
APP_PATH = PROJECT / "src" / "mf_faq" / "ui" / "app.py"

AMC_URL = "https://groww.in/mutual-funds/amc/hdfc-mutual-funds"
ELSS_URL = "https://groww.in/mutual-funds/hdfc-elss-tax-saver-fund-direct-plan-growth"
LARGE_CAP_URL = (
    "https://groww.in/mutual-funds/hdfc-large-cap-fund-direct-growth"
)
FLEXI_URL = "https://groww.in/mutual-funds/hdfc-equity-fund-direct-growth"
MID_URL = "https://groww.in/mutual-funds/hdfc-mid-cap-fund-direct-growth"
ELSS_TITLE = "HDFC ELSS Tax Saver Fund Direct Plan Growth"
NOW = datetime(2026, 11, 9, 10, 0, tzinfo=timezone.utc)

CLARIFY_OPTIONS = [
    {
        "scheme_id": "hdfc_large_cap",
        "title": "HDFC Large Cap Fund Direct Growth",
        "url": LARGE_CAP_URL,
    },
    {
        "scheme_id": "hdfc_flexi_cap",
        "title": "HDFC Flexi Cap Direct Plan Growth",
        "url": FLEXI_URL,
    },
    {
        "scheme_id": "hdfc_elss",
        "title": "HDFC ELSS Tax Saver Fund Direct Plan Growth",
        "url": ELSS_URL,
    },
    {
        "scheme_id": "hdfc_mid_cap",
        "title": "HDFC Mid Cap Fund Direct Growth",
        "url": MID_URL,
    },
]


def evidence(
    *,
    chunk_id: str = "elss_expense_ratio",
    text: str = f"{ELSS_TITLE} > Expense ratio: 1.20%",
) -> RetrievedEvidence:
    return RetrievedEvidence(
        chunk_id=chunk_id,
        doc_id="c4_hdfc_elss",
        url=ELSS_URL,
        title=ELSS_TITLE,
        section="Fund details",
        field="expense_ratio",
        score=0.91,
        text=text,
        fetched_at=NOW,
    )


def answered_response(
    *,
    answer: str = "The expense ratio is 1.20%.",
    citation_url: str = ELSS_URL,
    citation_title: str = ELSS_TITLE,
    trace_id: str = "a" * 32,
    evidence_items: list[RetrievedEvidence] | None = None,
) -> AnswerResponse:
    return AnswerResponse(
        trace_id=trace_id,
        status="answered",
        answer=answer,
        citation=Citation(url=citation_url, title=citation_title, section="Fund details"),
        last_updated="2026-11-09",
        evidence=evidence_items if evidence_items is not None else [evidence()],
        guardrails={
            "pii": "pass",
            "intent": "factual",
            "resolved_scheme": "hdfc_elss",
            "resolved_field": "expense_ratio",
            "sentences": 1,
            "fallback": False,
        },
        stage_timings_ms={"retrieval": 4.0, "generation": 12.0},
    )


def clarify_response(
    *,
    trace_id: str = "b" * 32,
    options: list[dict[str, str]] | None = None,
) -> AnswerResponse:
    return AnswerResponse(
        trace_id=trace_id,
        status="clarify",
        answer=(
            "Which fund do you mean: HDFC Large Cap, HDFC Flexi Cap, "
            "HDFC ELSS Tax Saver, or HDFC Mid Cap?"
        ),
        citation=None,
        guardrails={
            "resolved": "missing_scheme",
            "options": options if options is not None else CLARIFY_OPTIONS,
            "field": "expense_ratio",
        },
    )


def pii_response(*, trace_id: str = "c" * 32) -> AnswerResponse:
    return AnswerResponse(
        trace_id=trace_id,
        status="blocked_pii",
        answer=(
            "I can't accept PAN, Aadhaar, account numbers, OTPs, email addresses, "
            "or phone numbers. Please remove that information and ask your "
            "question again."
        ),
        citation=None,
        guardrails={"pii": "blocked", "pii_types": ["PAN", "OTP"]},
    )


def refusal_response(
    status: str = "refused_advice",
    *,
    trace_id: str = "d" * 32,
) -> AnswerResponse:
    answer = (
        "I can only provide factual information from the five indexed HDFC pages "
        "and cannot give investment advice or suitability guidance. For education, "
        "see the SEBI investor education page."
    )
    url = "https://investor.sebi.gov.in/"
    return AnswerResponse(
        trace_id=trace_id,
        status=status,
        answer=answer,
        citation=Citation(url=url, title="SEBI investor education"),
        guardrails={"intent": "advice"},
    )


class FakeAnswerService:
    """Return queued responses and optionally write minimal UI-inspector traces."""

    def __init__(
        self,
        responses: Iterable[AnswerResponse],
        *,
        trace_root: Path | None = None,
        raw_response: str = '{"status": "answered", "answer": "The expense ratio is 1.20%."}',
    ) -> None:
        self.responses = list(responses)
        self.calls: list[Any] = []
        self.trace_root = Path(trace_root) if trace_root else None
        self.raw_response = raw_response

    def answer(self, request):
        self.calls.append(request)
        if not self.responses:
            raise AssertionError("FakeAnswerService has no response queued")
        response = self.responses.pop(0)
        if self.trace_root is not None:
            self._write_traces(request, response)
        return response

    def _write_traces(self, request, response: AnswerResponse) -> None:
        root = self.trace_root
        assert root is not None
        blocked = response.status == "blocked_pii"
        prompt_path = root / "prompts" / f"{response.trace_id}.txt"
        retrieval_path = root / "retrieval" / f"{response.trace_id}.json"
        raw_path = root / "responses" / f"{response.trace_id}.txt"
        has_retrieval = response.status in {"answered", "not_found"}
        if has_retrieval:
            write_text(prompt_path, "SYSTEM:\nYou are a facts-only assistant.\n\nUSER:\nquestion")
            write_text(raw_path, f"--- RAW RESPONSE ---\n{self.raw_response}\n")
        write_json(
            root / "answers" / f"{response.trace_id}.json",
            {
                "trace_id": response.trace_id,
                "status": response.status,
                "query": None if blocked else request.query,
                "answer": response.answer,
                "citation_url": response.citation.url if response.citation else None,
                "evidence_ids": [item.chunk_id for item in response.evidence],
                "provider": "fake",
                "model": "fake-model",
                "prompt_hash": "fake-hash" if has_retrieval else "",
                "guardrails": response.guardrails,
                "usage": {},
                "latency_ms": 1.0,
                "fallback": False,
                "repair_attempted": False,
                "prompt_path": str(prompt_path) if has_retrieval else None,
                "retrieval_trace_path": str(retrieval_path) if has_retrieval else None,
                "stage_timings_ms": response.stage_timings_ms,
            },
        )
        if not has_retrieval:
            return

        candidates = [
            {
                "chunk_id": item.chunk_id,
                "doc_id": item.doc_id,
                "scheme_id": "hdfc_elss",
                "field": item.field,
                "section": item.section,
                "url": item.url,
                "score": item.score,
            }
            for item in response.evidence
        ]
        write_json(
            retrieval_path,
            {
                "trace_id": response.trace_id,
                "query": request.query,
                "resolved_scheme": "hdfc_elss",
                "resolved_field": "expense_ratio",
                "intent": "factual",
                "filters": {"scheme_id": "hdfc_elss", "field": "expense_ratio"},
                "filter_fallback": False,
                "candidates": candidates,
                "selected": [item.chunk_id for item in response.evidence],
                "latency_ms": 1.0,
            },
        )


class FakeCollection:
    def __init__(self, records: list[dict[str, Any]]) -> None:
        self.records = records

    def get(self, limit: int = 3, include=None):
        del include
        subset = self.records[:limit]
        return {
            "ids": [record["chunk_id"] for record in subset],
            "metadatas": [record["metadata"] for record in subset],
            "documents": [record["text"] for record in subset],
        }


class FakeStore:
    """Small stand-in for ``ChromaStore`` used by the Index inspector."""

    collection_name = "mf_faq_v1"

    def __init__(self, records: list[dict[str, Any]]) -> None:
        self.records = records
        self.collection = FakeCollection(records)

    def count(self) -> int:
        return len(self.records)

    def ids_where(self, where=None) -> list[str]:
        where = where or {}
        matches = []
        for record in self.records:
            metadata = record["metadata"]
            if all(metadata.get(key) == value for key, value in where.items()):
                matches.append(record["chunk_id"])
        return matches


def fake_store() -> FakeStore:
    return FakeStore(
        [
            {
                "chunk_id": "elss_expense_ratio",
                "metadata": {
                    "doc_id": "c4_hdfc_elss",
                    "scheme_id": "hdfc_elss",
                    "field": "expense_ratio",
                    "section": "Fund details",
                    "url": ELSS_URL,
                },
                "text": f"{ELSS_TITLE} > Expense ratio: 1.20%",
            },
            {
                "chunk_id": "large_cap_exit_load",
                "metadata": {
                    "doc_id": "c2_hdfc_large_cap",
                    "scheme_id": "hdfc_large_cap",
                    "field": "exit_load",
                    "section": "Fund details",
                    "url": LARGE_CAP_URL,
                },
                "text": "HDFC Large Cap Fund Direct Growth > Exit load: 1%",
            },
        ]
    )


def run_app(
    service: Any,
    *,
    settings: Settings | None = None,
    store: Any | None = None,
    timeout: float = 30.0,
) -> AppTest:
    test = AppTest.from_file(str(APP_PATH), default_timeout=timeout)
    test.session_state["settings"] = settings or Settings.load(CONFIG)
    test.session_state["answer_service"] = service
    if store is not None:
        test.session_state["chroma_store"] = store
    test.run(timeout=timeout)
    return test


def all_ui_text(test: AppTest) -> str:
    parts: list[str] = []
    for name in (
        "title",
        "header",
        "subheader",
        "markdown",
        "caption",
        "text",
        "text_area",
        "code",
        "json",
        "info",
        "warning",
        "error",
        "success",
        "table",
    ):
        collection = getattr(test, name, None)
        if collection is None:
            continue
        for item in collection:
            value = getattr(item, "value", None)
            if value is not None:
                parts.append(str(value))
    for collection in (test.button, test.link_button):
        for item in collection:
            parts.append(str(getattr(item, "label", "")))
    for item in test.dataframe:
        value = getattr(item, "value", None)
        if value is None:
            continue
        parts.append(
            value.to_string() if hasattr(value, "to_string") else str(value)
        )
    return "\n".join(parts)


def write_inspector_fixtures(base: Path) -> Settings:
    """Create small artifacts for every Pipeline sub-tab under ``base``."""
    (base / "data" / "manifests").mkdir(parents=True, exist_ok=True)
    (base / "data" / "processed").mkdir(parents=True, exist_ok=True)
    (base / "data" / "chunks").mkdir(parents=True, exist_ok=True)
    (base / "data" / "embeddings").mkdir(parents=True, exist_ok=True)
    (base / "docs").mkdir(parents=True, exist_ok=True)

    documents = []
    for index, doc_id in enumerate(
        [
            "c1_amc_hdfc",
            "c2_hdfc_large_cap",
            "c3_hdfc_flexi_cap",
            "c4_hdfc_elss",
            "c5_hdfc_mid_cap",
        ],
        start=1,
    ):
        raw_relative = f"data/raw/{doc_id}/snapshot.html"
        raw_path = base / raw_relative
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        write_text(
            raw_path,
            f"<html><body>raw {doc_id} <script>alert('raw')</script></body></html>",
        )
        documents.append(
            {
                "doc_id": doc_id,
                "url": AMC_URL if index == 1 else ELSS_URL,
                "title": f"Fixture {doc_id}",
                "page_type": "amc" if index == 1 else "scheme",
                "scheme_id": "amc_hdfc" if index == 1 else "hdfc_elss",
                "fetched_at": NOW.isoformat(),
                "http_status": "200",
                "content_hash": f"sha256:fixture-{index}",
                "text": f"Fixture cleaned text for {doc_id}.",
            }
        )
    write_jsonl(base / "data" / "processed" / "documents.jsonl", documents)

    write_json(
        base / "data" / "manifests" / "load_manifest.json",
        {
            "created_at": NOW.isoformat(),
            "config_hash": "fixture-config",
            "document_count": 5,
            "failure_count": 0,
            "documents": [
                {
                    "doc_id": document["doc_id"],
                    "url": document["url"],
                    "title": document["title"],
                    "scheme_id": document["scheme_id"],
                    "fetched_at": document["fetched_at"],
                    "http_status": 200,
                    "content_hash": document["content_hash"],
                    "renderer": "httpx",
                    "word_count": 100 + index,
                    "coverage": {
                        "expense_ratio": True,
                        "exit_load": index % 2 == 0,
                        "minimum_sip": False,
                        "lock_in": False,
                        "riskometer": True,
                        "benchmark": True,
                        "statement_download": False,
                    },
                    "warnings": [],
                    "raw_path": f"data/raw/{document['doc_id']}/snapshot.html",
                }
                for index, document in enumerate(documents)
            ],
        },
    )

    chunk_rows = [
        {
            "chunk_id": "elss_expense_ratio",
            "doc_id": "c4_hdfc_elss",
            "url": ELSS_URL,
            "title": ELSS_TITLE,
            "scheme_id": "hdfc_elss",
            "page_type": "scheme",
            "section_path": ["Fund details", "Expense ratio"],
            "field": "expense_ratio",
            "chunk_type": "kv_pair",
            "content_class": "factual",
            "indexed": True,
            "exclusion_reason": None,
            "text": f"{ELSS_TITLE} > Expense ratio: 1.20%",
            "token_count": 12,
            "content_hash": "sha256:fixture-chunk-1",
            "fetched_at": NOW.isoformat(),
            "oversize": False,
        },
        {
            "chunk_id": "large_cap_exit_load",
            "doc_id": "c2_hdfc_large_cap",
            "url": LARGE_CAP_URL,
            "title": "HDFC Large Cap Fund Direct Growth",
            "scheme_id": "hdfc_large_cap",
            "page_type": "scheme",
            "section_path": ["Fund details", "Exit load"],
            "field": "exit_load",
            "chunk_type": "kv_pair",
            "content_class": "factual",
            "indexed": True,
            "exclusion_reason": None,
            "text": "HDFC Large Cap Fund Direct Growth > Exit load: 1%",
            "token_count": 11,
            "content_hash": "sha256:fixture-chunk-2",
            "fetched_at": NOW.isoformat(),
            "oversize": False,
        },
    ]
    write_jsonl(base / "data" / "chunks" / "chunks.jsonl", chunk_rows)
    write_jsonl(
        base / "data" / "chunks" / "excluded.jsonl",
        [
            {
                "chunk_id": "excluded_rating",
                "doc_id": "c2_hdfc_large_cap",
                "field": "other",
                "content_class": "boilerplate",
                "exclusion_reason": "boilerplate: rating",
                "text": "Rating row",
            }
        ],
    )
    write_json(
        base / "data" / "chunks" / "chunk_stats.json",
        {
            "generated_at": NOW.isoformat(),
            "config_hash": "fixture-config",
            "chunk_count": 2,
            "indexed_count": 2,
            "excluded_count": 1,
            "oversize_count": 0,
            "invariant_violations": 0,
            "tokens": {"min": 11, "max": 12, "mean": 11.5, "total": 23},
            "by_chunk_type": {"kv_pair": 2},
            "by_content_class": {"factual": 2},
            "by_field": {"expense_ratio": 1, "exit_load": 1},
        },
    )

    matrix = np.zeros((2, 384), dtype=np.float32)
    matrix[:, 0] = 1.0
    np.save(base / "data" / "embeddings" / "embeddings.npy", matrix)
    write_json(
        base / "data" / "embeddings" / "embedding_manifest.json",
        {
            "model": "sentence-transformers/all-MiniLM-L6-v2",
            "dimension": 384,
            "max_seq_length": 256,
            "count": 2,
            "normalized": True,
            "config_hash": "fixture-config",
            "created_at": NOW.isoformat(),
        },
    )
    write_json(
        base / "data" / "embeddings" / "embedding_preview.json",
        {
            "model": "sentence-transformers/all-MiniLM-L6-v2",
            "dimension": 384,
            "count": 2,
            "normalized": True,
            "generated_at": NOW.isoformat(),
            "preview": [
                {
                    "chunk_id": "elss_expense_ratio",
                    "first_eight_dimensions": [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
                    "norm": 1.0,
                    "text_preview": "fixture preview",
                }
            ],
        },
    )
    write_json(
        base / "data" / "index_manifest.json",
        {
            "collection": "mf_faq_v1",
            "count": 2,
            "model": "sentence-transformers/all-MiniLM-L6-v2",
            "dimension": 384,
            "distance": "cosine",
            "config_hash": "fixture-config",
            "created_at": NOW.isoformat(),
        },
    )

    fixture_trace = "0" * 32
    trace_root = base / "data" / "traces"
    write_json(
        trace_root / "answers" / f"{fixture_trace}.json",
        {
            "trace_id": fixture_trace,
            "status": "answered",
            "query": "Fixture question about HDFC ELSS",
            "answer": "The expense ratio is 1.20%.",
            "citation_url": ELSS_URL,
            "evidence_ids": ["elss_expense_ratio"],
            "provider": "fixture",
            "model": "fixture-model",
            "prompt_hash": "fixture-hash",
            "guardrails": {"pii": "pass", "resolved_field": "expense_ratio"},
            "usage": {},
            "latency_ms": 2.0,
            "fallback": False,
            "repair_attempted": False,
            "prompt_path": str(trace_root / "prompts" / f"{fixture_trace}.txt"),
            "retrieval_trace_path": str(
                trace_root / "retrieval" / f"{fixture_trace}.json"
            ),
            "stage_timings_ms": {"retrieval": 1.0, "generation": 1.0},
        },
    )
    write_json(
        trace_root / "retrieval" / f"{fixture_trace}.json",
        {
            "trace_id": fixture_trace,
            "query": "Fixture question about HDFC ELSS",
            "resolved_scheme": "hdfc_elss",
            "resolved_field": "expense_ratio",
            "intent": "factual",
            "filters": {"scheme_id": "hdfc_elss", "field": "expense_ratio"},
            "filter_fallback": False,
            "candidates": [
                {
                    "chunk_id": "elss_expense_ratio",
                    "doc_id": "c4_hdfc_elss",
                    "scheme_id": "hdfc_elss",
                    "field": "expense_ratio",
                    "section": "Fund details",
                    "url": ELSS_URL,
                    "score": 0.91,
                }
            ],
            "selected": ["elss_expense_ratio"],
            "latency_ms": 1.0,
        },
    )
    write_text(
        trace_root / "prompts" / f"{fixture_trace}.txt",
        "SYSTEM:\nFixture prompt <script>alert('prompt')</script>\n\nUSER:\nfixture",
    )
    write_text(
        trace_root / "responses" / f"{fixture_trace}.txt",
        "--- RAW RESPONSE ---\n"
        '{"status": "answered", "answer": "<script>alert(\'raw\')</script>"}\n',
    )

    # A repo-owned source registry keeps the Sources tab renderable.
    write_text(
        base / "docs" / "sources.yaml",
        "\n".join(
            [
                "corpus:",
                "  - {id: c1_amc_hdfc, scheme_id: amc_hdfc, page_type: amc, "
                f'title: "HDFC Mutual Fund", url: "{AMC_URL}"}}',
                "  - {id: c4_hdfc_elss, scheme_id: hdfc_elss, page_type: scheme, "
                f'title: "{ELSS_TITLE}", url: "{ELSS_URL}"}}',
                "references:",
                "  - {id: ref_sebi_investor, title: \"SEBI investor education\", "
                'url: "https://investor.sebi.gov.in/"}',
            ]
        )
        + "\n",
    )
    write_text(
        base / "docs" / "sample_qa.md",
        "# Sample Q&A\n\nFixture evaluation table.\n\n"
        "## Recorded responses\n\nsecret-answer-marker\n",
    )

    return Settings.load(CONFIG, base_dir=base)


def json_dump(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False, default=str)
