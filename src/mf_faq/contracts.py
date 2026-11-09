"""Pydantic contracts shared by ingestion, retrieval, generation, and UI.

These models are the schema boundary for every JSON/JSONL artifact and for
the answer response. Keep them free of I/O and provider-specific behaviour.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

AnswerStatus = Literal[
    "answered",
    "clarify",
    "not_found",
    "refused_advice",
    "refused_performance",
    "out_of_scope",
    "blocked_pii",
    "error",
]

ChunkType = Literal["kv_pair", "table_row", "list_item", "paragraph", "other"]
ContentClass = Literal["factual", "performance", "boilerplate", "navigation"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourcePage(_Model):
    doc_id: str = Field(min_length=1)
    url: str = Field(min_length=1)
    title: str = Field(min_length=1)
    page_type: Literal["amc", "scheme"]
    scheme_id: str = Field(min_length=1)
    fetched_at: datetime
    http_status: int = Field(ge=100, le=599)
    content_hash: str = Field(min_length=1)
    text: str = Field(min_length=1)

    @field_validator("url")
    @classmethod
    def _absolute_url(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("url must be absolute http(s)")
        return value


class Chunk(_Model):
    chunk_id: str = Field(min_length=1)
    doc_id: str = Field(min_length=1)
    url: str = Field(min_length=1)
    title: str = Field(min_length=1)
    scheme_id: str = Field(min_length=1)
    page_type: Literal["amc", "scheme"] | None = None
    section_path: list[str] = Field(default_factory=list)
    field: str = Field(min_length=1)
    chunk_type: ChunkType
    content_class: ContentClass
    indexed: bool = True
    exclusion_reason: str | None = None
    text: str = Field(min_length=1)
    token_count: int = Field(ge=0)
    content_hash: str = Field(min_length=1)
    fetched_at: datetime | None = None
    oversize: bool = False

    @field_validator("exclusion_reason")
    @classmethod
    def _reason_required_when_excluded(cls, value: str | None, info: Any) -> str | None:
        indexed = info.data.get("indexed")
        if indexed is False and not value:
            raise ValueError("excluded chunks require exclusion_reason")
        return value


class EmbeddingManifest(_Model):
    model: str = Field(min_length=1)
    dimension: int = Field(gt=0)
    max_seq_length: int = Field(gt=0)
    count: int = Field(ge=0)
    normalized: bool
    config_hash: str = Field(min_length=1)
    created_at: datetime


class IndexManifest(_Model):
    collection: str = Field(min_length=1)
    count: int = Field(ge=0)
    model: str = Field(min_length=1)
    dimension: int = Field(gt=0)
    distance: str = Field(min_length=1)
    config_hash: str = Field(min_length=1)
    created_at: datetime


class QueryRequest(_Model):
    query: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    last_scheme: str | None = None
    turn_count: int = Field(default=0, ge=0)

    @field_validator("query")
    @classmethod
    def _meaningful_query(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("query cannot be blank")
        return value


class RetrievedEvidence(_Model):
    chunk_id: str = Field(min_length=1)
    doc_id: str = Field(min_length=1)
    url: str = Field(min_length=1)
    title: str = Field(min_length=1)
    section: str = ""
    field: str = Field(min_length=1)
    score: float
    text: str = Field(min_length=1)
    fetched_at: datetime | None = None


class Citation(_Model):
    url: str = Field(min_length=1)
    title: str = Field(min_length=1)
    section: str | None = None


class AnswerResponse(_Model):
    trace_id: str = Field(min_length=1)
    status: AnswerStatus
    answer: str = ""
    citation: Citation | None = None
    last_updated: str | None = None
    evidence: list[RetrievedEvidence] = Field(default_factory=list)
    guardrails: dict[str, Any] = Field(default_factory=dict)
    stage_timings_ms: dict[str, float] = Field(default_factory=dict)


class RetrievalTrace(_Model):
    trace_id: str = Field(min_length=1)
    query: str
    resolved_scheme: str | None = None
    resolved_field: str | None = None
    intent: str = ""
    filters: dict[str, Any] = Field(default_factory=dict)
    filter_fallback: bool = False
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    selected: list[str] = Field(default_factory=list)
    latency_ms: float = Field(default=0.0, ge=0)


class AnswerTrace(_Model):
    trace_id: str = Field(min_length=1)
    status: AnswerStatus | None = None
    query: str | None = None
    answer: str = ""
    citation_url: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    provider: str = ""
    model: str = ""
    prompt_hash: str = ""
    guardrails: dict[str, Any] = Field(default_factory=dict)
    usage: dict[str, Any] = Field(default_factory=dict)
    latency_ms: float = Field(default=0.0, ge=0)
    fallback: bool = False
    repair_attempted: bool = False
    prompt_path: str | None = None
    retrieval_trace_path: str | None = None
    stage_timings_ms: dict[str, float] = Field(default_factory=dict)
