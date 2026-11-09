"""Answer state machine, JSON parsing, validation, fallback, and traces.

The service follows the decision order in ``implementation.md`` §7.1 exactly:
length → PII → intent → resolve → clarify → retrieve → generate → validate →
citation → trace. A trace is written for every status; PII traces omit the query
and never contain the raw text.

See ``implementation.md`` §7 and §9.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from mf_faq.config import Settings
from mf_faq.contracts import (
    AnswerResponse,
    AnswerTrace,
    Citation,
    QueryRequest,
    RetrievalTrace,
    RetrievedEvidence,
)
from mf_faq.embeddings.embedder import ChunkEmbedder
from mf_faq.generation.citation import (
    ReferenceRegistry,
    load_registry,
    resolve_citation,
)
from mf_faq.generation.client import LLMClient, LLMError, build_llm_client
from mf_faq.generation.prompts import (
    SYSTEM_PROMPT,
    build_messages,
    build_repair_prompt,
    prompt_hash,
)
from mf_faq.guardrails.intent import screen_intent
from mf_faq.guardrails.pii import contains_pii, log_pii_block, screen_pii
from mf_faq.guardrails.sentences import count_sentences
from mf_faq.guardrails.validators import (
    ValidationResult,
    ensure_valid_answer,
    extractive_answer,
)
from mf_faq.index.chroma_store import ChromaStore
from mf_faq.io_utils import write_json, write_text
from mf_faq.retrieval.resolver import Resolution, resolve_query
from mf_faq.retrieval.retriever import RetrievalResult, Retriever

__all__ = [
    "AnswerParseError",
    "AnswerService",
    "ModelAnswer",
    "parse_model_answer",
]

ALLOWED_MODEL_STATUSES = frozenset(
    {"answered", "not_found", "refused_advice", "refused_performance"}
)

CLARIFY_MISSING_SCHEME = (
    "Which fund do you mean: HDFC Large Cap, HDFC Flexi Cap, HDFC ELSS Tax Saver, "
    "or HDFC Mid Cap?"
)
NOT_FOUND_COPY = "That fact is not available on the five indexed HDFC pages."
REFUSED_ADVICE_COPY = (
    "I can only provide factual information from the five indexed HDFC pages and "
    "cannot give investment advice or suitability guidance. For education, see the "
    "SEBI investor education page."
)
REFUSED_PERFORMANCE_COPY = (
    "I cannot provide performance figures. Please see the official HDFC factsheet "
    "for scheme-level details."
)
OUT_OF_SCOPE_COPY = (
    "This assistant covers only the HDFC AMC overview and the HDFC Large Cap, "
    "Flexi Cap, ELSS Tax Saver, and Mid Cap funds."
)
BLOCKED_PII_COPY = (
    "I can't accept PAN, Aadhaar, account numbers, OTPs, email addresses, or phone "
    "numbers. Please remove that information and ask your question again."
)
ERROR_COPY = "I couldn't complete that request. Please try again or ask a simpler factual question."
TOO_LONG_COPY = "Please shorten your question to 500 characters or fewer."

REFUSAL_COPY = {
    "refused_advice": REFUSED_ADVICE_COPY,
    "refused_performance": REFUSED_PERFORMANCE_COPY,
    "out_of_scope": OUT_OF_SCOPE_COPY,
}

_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


class AnswerParseError(ValueError):
    """The model output was not a valid answer object."""


@dataclass(frozen=True)
class ModelAnswer:
    status: str
    answer: str = ""
    citation_url: str | None = None
    citation_title: str | None = None
    section: str | None = None
    field: str | None = None
    confidence: float = 0.0
    raw: str = ""


def _extract_balanced_object(text: str) -> str | None:
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def _load_json_object(text: str) -> dict[str, Any]:
    candidates = [text.strip(), _CODE_FENCE_RE.sub("", text.strip()).strip()]
    balanced = _extract_balanced_object(text)
    if balanced:
        candidates.append(balanced)
    for candidate in candidates:
        if not candidate:
            continue
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    raise AnswerParseError("model output is not a JSON object")


def parse_model_answer(text: str) -> ModelAnswer:
    """Parse strict JSON, strip fences, then extract the first balanced object."""
    payload = _load_json_object(text or "")
    status = str(payload.get("status", "")).strip().lower()
    if status not in ALLOWED_MODEL_STATUSES:
        raise AnswerParseError(f"invalid model status {status!r}")

    confidence: float = 0.0
    try:
        confidence = float(payload.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0

    return ModelAnswer(
        status=status,
        answer=str(payload.get("answer", "") or ""),
        citation_url=payload.get("citation_url") or None,
        citation_title=payload.get("citation_title") or None,
        section=payload.get("section") or None,
        field=payload.get("field") or None,
        confidence=confidence,
        raw=text,
    )


class AnswerService:
    """Orchestrate guardrails, retrieval, generation, validation, and traces."""

    def __init__(
        self,
        settings: Settings,
        *,
        store: ChromaStore | None = None,
        embedder: ChunkEmbedder | None = None,
        llm: LLMClient | None = None,
        retriever: Retriever | None = None,
        registry: ReferenceRegistry | None = None,
        trace_root: str | Path | None = None,
        clock=time.perf_counter,
    ) -> None:
        self.settings = settings
        self._store = store
        self._embedder = embedder
        self._llm = llm
        self._retriever = retriever
        self.registry = registry or load_registry(settings)
        self.trace_root = (
            Path(trace_root)
            if trace_root is not None
            else settings.resolved_path("data/traces")
        )
        self._clock = clock

    @property
    def retriever(self) -> Retriever:
        if self._retriever is None:
            self._retriever = Retriever(
                self.settings,
                store=self._store,
                embedder=self._embedder,
            )
        return self._retriever

    @property
    def llm(self) -> LLMClient:
        if self._llm is None:
            self._llm = build_llm_client(self.settings)
        return self._llm

    # ------------------------------------------------------------------ traces
    def _path(self, kind: str, trace_id: str, suffix: str) -> Path:
        return self.trace_root / kind / f"{trace_id}.{suffix}"

    def _write_retrieval_trace(self, trace: RetrievalTrace) -> Path:
        path = self._path("retrieval", trace.trace_id, "json")
        write_json(path, trace)
        return path

    def _write_prompt(self, trace_id: str, text: str) -> Path:
        path = self._path("prompts", trace_id, "txt")
        write_text(path, text)
        return path

    def _append_prompt(self, trace_id: str, text: str) -> Path:
        path = self._path("prompts", trace_id, "txt")
        existing = path.read_text(encoding="utf-8") if path.is_file() else ""
        write_text(path, f"{existing}\n\n--- REPAIR ---\n\n{text}")
        return path

    def _append_raw_response(
        self,
        trace_id: str,
        text: str,
        *,
        section: str,
    ) -> Path | None:
        """Append a raw model response for the read-only UI inspector.

        The raw text is never stored when it contains PII, so a blocked value
        cannot reappear in the Pipeline tab. It lives outside ``AnswerTrace``
        because traces are structured and redacted.
        """
        if not text or contains_pii(text):
            return None
        path = self._path("responses", trace_id, "txt")
        existing = path.read_text(encoding="utf-8") if path.is_file() else ""
        block = f"--- {section} ---\n{text}\n"
        write_text(path, f"{existing}\n{block}" if existing else block)
        return path

    def _write_answer_trace(
        self,
        *,
        trace_id: str,
        status: str,
        query: str | None,
        answer: str,
        citation: Citation | None,
        evidence: Sequence[RetrievedEvidence],
        guardrails: dict[str, Any],
        stage_timings_ms: dict[str, float],
        started: float,
        provider: str = "",
        model: str = "",
        prompt_sha: str = "",
        usage: dict[str, Any] | None = None,
        fallback: bool = False,
        repair_attempted: bool = False,
        prompt_path: Path | None = None,
        retrieval_path: Path | None = None,
    ) -> Path:
        trace = AnswerTrace(
            trace_id=trace_id,
            status=status,
            query=query,
            answer=answer,
            citation_url=citation.url if citation else None,
            evidence_ids=[item.chunk_id for item in evidence],
            provider=provider,
            model=model,
            prompt_hash=prompt_sha,
            guardrails=guardrails,
            usage=usage or {},
            latency_ms=(self._clock() - started) * 1000.0,
            fallback=fallback,
            repair_attempted=repair_attempted,
            prompt_path=str(prompt_path) if prompt_path else None,
            retrieval_trace_path=str(retrieval_path) if retrieval_path else None,
            stage_timings_ms=stage_timings_ms,
        )
        path = self._path("answers", trace_id, "json")
        write_json(path, trace)
        return path

    @staticmethod
    def _last_updated(evidence: Sequence[RetrievedEvidence]) -> str | None:
        dates = [item.fetched_at for item in evidence if item.fetched_at is not None]
        if not dates:
            return None
        return max(dates).date().isoformat()

    def _options(self, options: Sequence[Any]) -> list[dict[str, str]]:
        return [
            {"scheme_id": item.scheme_id, "title": item.title, "url": item.url}
            for item in options
        ]

    # ------------------------------------------------------------------ answer
    def answer(self, request: QueryRequest) -> AnswerResponse:
        started = self._clock()
        trace_id = uuid.uuid4().hex
        query = request.query.strip()
        pii = screen_pii(query)
        trace_query = None if pii.blocked else query

        try:
            return self._answer(
                request=request,
                trace_id=trace_id,
                query=query,
                started=started,
                trace_query=trace_query,
            )
        except Exception:
            # Fail safe: never leak an exception message or provider payload.
            citation = resolve_citation(
                status="error",
                registry=self.registry,
                resolved_scheme=None,
            )
            guardrails = {"internal": "error"}
            try:
                self._write_answer_trace(
                    trace_id=trace_id,
                    status="error",
                    query=trace_query,
                    answer=ERROR_COPY,
                    citation=citation,
                    evidence=[],
                    guardrails=guardrails,
                    stage_timings_ms={"total": (self._clock() - started) * 1000.0},
                    started=started,
                )
            except Exception:
                pass
            return AnswerResponse(
                trace_id=trace_id,
                status="error",
                answer=ERROR_COPY,
                citation=citation,
                guardrails=guardrails,
                stage_timings_ms={"total": (self._clock() - started) * 1000.0},
            )

    def _answer(
        self,
        *,
        request: QueryRequest,
        trace_id: str,
        query: str,
        started: float,
        trace_query: str | None,
    ) -> AnswerResponse:
        timings: dict[str, float] = {}

        # 1. Length check.
        limit = self.settings.app.max_query_chars
        if len(query) > limit:
            stage_started = self._clock()
            citation = resolve_citation(status="error", registry=self.registry)
            timings["length"] = (self._clock() - stage_started) * 1000.0
            guardrails = {"length": "too_long", "limit": limit}
            self._write_answer_trace(
                trace_id=trace_id,
                status="error",
                query=trace_query,
                answer=TOO_LONG_COPY,
                citation=citation,
                evidence=[],
                guardrails=guardrails,
                stage_timings_ms=timings,
                started=started,
            )
            return AnswerResponse(
                trace_id=trace_id,
                status="error",
                answer=TOO_LONG_COPY,
                citation=citation,
                guardrails=guardrails,
                stage_timings_ms=timings,
            )

        # 2. PII screen.
        stage_started = self._clock()
        pii = screen_pii(query)
        timings["pii"] = (self._clock() - stage_started) * 1000.0
        if pii.blocked and self.settings.guardrails.pii_block:
            guardrails = {
                "pii": "blocked",
                "pii_types": list(pii.pii_types),
                "query_hash": pii.salted_hash,
            }
            try:
                log_pii_block(
                    pii,
                    trace_id=trace_id,
                    session_id=request.session_id,
                    log_path=self.trace_root / "security" / "pii_blocks.jsonl",
                )
            except OSError:
                guardrails["security_log"] = "unavailable"
            # Minimal trace: no query text, no evidence, no prompt.
            self._write_answer_trace(
                trace_id=trace_id,
                status="blocked_pii",
                query=None,
                answer=BLOCKED_PII_COPY,
                citation=None,
                evidence=[],
                guardrails=guardrails,
                stage_timings_ms=timings,
                started=started,
            )
            return AnswerResponse(
                trace_id=trace_id,
                status="blocked_pii",
                answer=BLOCKED_PII_COPY,
                citation=None,
                guardrails=guardrails,
                stage_timings_ms=timings,
            )

        # 3. Intent screen.
        stage_started = self._clock()
        intent = screen_intent(query)
        timings["intent"] = (self._clock() - stage_started) * 1000.0
        if intent.status:
            status = intent.status
            answer = REFUSAL_COPY[status]
            citation = resolve_citation(status=status, registry=self.registry)
            guardrails = {
                "intent": intent.intent,
                "matched": list(intent.matched),
            }
            self._write_answer_trace(
                trace_id=trace_id,
                status=status,
                query=trace_query,
                answer=answer,
                citation=citation,
                evidence=[],
                guardrails=guardrails,
                stage_timings_ms=timings,
                started=started,
            )
            return AnswerResponse(
                trace_id=trace_id,
                status=status,
                answer=answer,
                citation=citation,
                guardrails=guardrails,
                stage_timings_ms=timings,
            )

        # 4. Resolve scheme + field and clarify when needed.
        stage_started = self._clock()
        resolution = resolve_query(
            query,
            settings=self.settings,
            last_scheme=request.last_scheme,
            turn_count=request.turn_count,
        )
        timings["resolve"] = (self._clock() - stage_started) * 1000.0

        if resolution.needs_clarification:
            if resolution.ambiguous:
                names = ", ".join(item.title for item in resolution.ambiguous)
                answer = f"Please ask about one fund at a time: {names}."
                options = resolution.ambiguous
                guardrails = {
                    "resolved": "ambiguous",
                    "options": self._options(options),
                    "field": resolution.field,
                }
            else:
                answer = CLARIFY_MISSING_SCHEME
                guardrails = {
                    "resolved": "missing_scheme",
                    "options": self._options(resolution.clarify_options),
                    "field": resolution.field,
                }
            self._write_answer_trace(
                trace_id=trace_id,
                status="clarify",
                query=trace_query,
                answer=answer,
                citation=None,
                evidence=[],
                guardrails=guardrails,
                stage_timings_ms=timings,
                started=started,
            )
            return AnswerResponse(
                trace_id=trace_id,
                status="clarify",
                answer=answer,
                citation=None,
                guardrails=guardrails,
                stage_timings_ms=timings,
            )

        # 5. Retrieve.
        stage_started = self._clock()
        retrieval = self.retriever.retrieve(
            query,
            trace_id=trace_id,
            intent=intent.intent,
            resolution=resolution,
        )
        timings["retrieval"] = (self._clock() - stage_started) * 1000.0
        retrieval_path = self._write_retrieval_trace(retrieval.trace)

        if not retrieval.evidence:
            status = "not_found"
            citation = resolve_citation(
                status=status,
                registry=self.registry,
                evidence=[],
                resolved_scheme=resolution.scheme_id,
                resolved_field=resolution.field,
            )
            guardrails = {
                "retrieval": "empty",
                "filter_fallback": retrieval.filter_fallback,
                "resolved_scheme": resolution.scheme_id,
                "resolved_field": resolution.field,
            }
            self._write_answer_trace(
                trace_id=trace_id,
                status=status,
                query=trace_query,
                answer=NOT_FOUND_COPY,
                citation=citation,
                evidence=[],
                guardrails=guardrails,
                stage_timings_ms=timings,
                started=started,
                retrieval_path=retrieval_path,
            )
            return AnswerResponse(
                trace_id=trace_id,
                status=status,
                answer=NOT_FOUND_COPY,
                citation=citation,
                last_updated=self._last_updated([]),
                guardrails=guardrails,
                stage_timings_ms=timings,
            )

        evidence = retrieval.evidence
        system, user = build_messages(query, evidence)
        prompt_sha = prompt_hash(system, user)
        prompt_path = self._write_prompt(trace_id, f"SYSTEM:\n{system}\n\nUSER:\n{user}")

        # 6. Generate.
        stage_started = self._clock()
        try:
            completion = self.llm.complete(system=system, user=user)
        except LLMError:
            timings["generation"] = (self._clock() - stage_started) * 1000.0
            return self._generation_error(
                trace_id=trace_id,
                query=trace_query,
                resolution=resolution,
                evidence=evidence,
                guardrails={
                    "generation": "error",
                    "resolved_scheme": resolution.scheme_id,
                },
                timings=timings,
                started=started,
                prompt_sha=prompt_sha,
                prompt_path=prompt_path,
                retrieval_path=retrieval_path,
                provider=self.settings.llm.provider,
                model=self.settings.llm.model,
            )
        timings["generation"] = (self._clock() - stage_started) * 1000.0
        self._append_raw_response(
            trace_id, completion.text, section="RAW RESPONSE"
        )

        repair_used = False
        parse_error = ""
        try:
            model_answer = parse_model_answer(completion.text)
        except AnswerParseError as exc:
            parse_error = str(exc)
            model_answer = None

        if (
            model_answer is None
            and self.settings.guardrails.repair_attempts > 0
            and not contains_pii(completion.text)
        ):
            repair_used = True
            repair_prompt = build_repair_prompt(
                system=system,
                user=user,
                invalid_output=completion.text,
                issues=[parse_error or "invalid output"],
            )
            self._append_prompt(trace_id, f"USER:\n{repair_prompt}")
            try:
                repaired = self.llm.complete(system=SYSTEM_PROMPT, user=repair_prompt)
                self._append_raw_response(
                    trace_id, repaired.text, section="REPAIR RESPONSE"
                )
                model_answer = parse_model_answer(repaired.text)
                completion = repaired
            except (LLMError, AnswerParseError):
                model_answer = None

        # 7. Parse failure → extractive fallback.
        if model_answer is None:
            fallback = extractive_answer(evidence[0])
            validation = ensure_valid_answer(
                fallback,
                max_sentences=self.settings.guardrails.max_sentences,
                evidence_texts=[item.text for item in evidence],
                check_numbers=True,
                repair=None,
                fallback_text=fallback,
                extractive_fallback=self.settings.guardrails.extractive_fallback,
            )
            if validation.ok:
                citation = resolve_citation(
                    status="answered",
                    registry=self.registry,
                    evidence=evidence,
                    resolved_scheme=resolution.scheme_id,
                )
                if citation is None:
                    return self._not_found_response(
                        trace_id=trace_id,
                        query=trace_query,
                        resolution=resolution,
                        evidence=evidence,
                        guardrails={"citation": "invalid_evidence"},
                        timings=timings,
                        started=started,
                        retrieval_path=retrieval_path,
                    )
                guardrails = {
                    "parse": "failed",
                    "fallback": True,
                    "repair_attempted": repair_used,
                    "resolved_scheme": resolution.scheme_id,
                    "resolved_field": resolution.field,
                    "sentences": count_sentences(validation.answer),
                }
                self._write_answer_trace(
                    trace_id=trace_id,
                    status="answered",
                    query=trace_query,
                    answer=validation.answer,
                    citation=citation,
                    evidence=evidence,
                    guardrails=guardrails,
                    stage_timings_ms=timings,
                    started=started,
                    provider=completion.provider,
                    model=completion.model,
                    prompt_sha=prompt_sha,
                    usage=completion.usage,
                    fallback=True,
                    repair_attempted=repair_used,
                    prompt_path=prompt_path,
                    retrieval_path=retrieval_path,
                )
                return AnswerResponse(
                    trace_id=trace_id,
                    status="answered",
                    answer=validation.answer,
                    citation=citation,
                    last_updated=self._last_updated(evidence),
                    evidence=evidence,
                    guardrails=guardrails,
                    stage_timings_ms=timings,
                )
            return self._not_found_response(
                trace_id=trace_id,
                query=trace_query,
                resolution=resolution,
                evidence=evidence,
                guardrails={"parse": "failed", "fallback": "invalid"},
                timings=timings,
                started=started,
                retrieval_path=retrieval_path,
            )

        # 8. Model refusal / not_found statuses.
        if model_answer.status in {"refused_advice", "refused_performance"}:
            status = model_answer.status
            answer = REFUSAL_COPY[status]
            citation = resolve_citation(status=status, registry=self.registry)
            guardrails = {"model_status": status, "resolved_scheme": resolution.scheme_id}
            self._write_answer_trace(
                trace_id=trace_id,
                status=status,
                query=trace_query,
                answer=answer,
                citation=citation,
                evidence=[],
                guardrails=guardrails,
                stage_timings_ms=timings,
                started=started,
                provider=completion.provider,
                model=completion.model,
                prompt_sha=prompt_sha,
                usage=completion.usage,
                repair_attempted=repair_used,
                prompt_path=prompt_path,
                retrieval_path=retrieval_path,
            )
            return AnswerResponse(
                trace_id=trace_id,
                status=status,
                answer=answer,
                citation=citation,
                guardrails=guardrails,
                stage_timings_ms=timings,
            )

        if model_answer.status == "not_found":
            return self._not_found_response(
                trace_id=trace_id,
                query=trace_query,
                resolution=resolution,
                evidence=evidence,
                guardrails={"model_status": "not_found"},
                timings=timings,
                started=started,
                retrieval_path=retrieval_path,
                provider=completion.provider,
                model=completion.model,
                prompt_sha=prompt_sha,
                usage=completion.usage,
                prompt_path=prompt_path,
            )

        # 9. Validate the answered text, with one repair then extractive fallback.
        def repair_answer(clean: str, issues: list[str]) -> str:
            nonlocal repair_used
            if repair_used or self.settings.guardrails.repair_attempts < 1:
                return ""
            # Never send PII back to the provider or into the prompt trace.
            if "pii_echo" in issues or contains_pii(clean):
                return ""
            repair_used = True
            repair_prompt = build_repair_prompt(
                system=system,
                user=user,
                invalid_output=clean,
                issues=issues,
            )
            self._append_prompt(trace_id, f"USER:\n{repair_prompt}")
            try:
                repaired = self.llm.complete(system=SYSTEM_PROMPT, user=repair_prompt)
                self._append_raw_response(
                    trace_id, repaired.text, section="REPAIR RESPONSE"
                )
                parsed = parse_model_answer(repaired.text)
                if parsed.status == "answered":
                    return parsed.answer
            except (LLMError, AnswerParseError):
                return ""
            return ""

        fallback_text = extractive_answer(evidence[0])
        validation = ensure_valid_answer(
            model_answer.answer,
            max_sentences=self.settings.guardrails.max_sentences,
            evidence_texts=[item.text for item in evidence],
            check_numbers=True,
            repair=repair_answer if not repair_used else None,
            fallback_text=fallback_text,
            extractive_fallback=self.settings.guardrails.extractive_fallback,
        )

        if not validation.ok:
            forced = validation.status_override
            if forced:
                status = forced
                answer = REFUSAL_COPY[status]
                citation = resolve_citation(status=status, registry=self.registry)
                guardrails = {
                    "validation": list(validation.issues),
                    "forced_refusal": status,
                    "resolved_scheme": resolution.scheme_id,
                }
                self._write_answer_trace(
                    trace_id=trace_id,
                    status=status,
                    query=trace_query,
                    answer=answer,
                    citation=citation,
                    evidence=[],
                    guardrails=guardrails,
                    stage_timings_ms=timings,
                    started=started,
                    provider=completion.provider,
                    model=completion.model,
                    prompt_sha=prompt_sha,
                    usage=completion.usage,
                    repair_attempted=repair_used,
                    prompt_path=prompt_path,
                    retrieval_path=retrieval_path,
                )
                return AnswerResponse(
                    trace_id=trace_id,
                    status=status,
                    answer=answer,
                    citation=citation,
                    guardrails=guardrails,
                    stage_timings_ms=timings,
                )
            return self._not_found_response(
                trace_id=trace_id,
                query=trace_query,
                resolution=resolution,
                evidence=evidence,
                guardrails={"validation": list(validation.issues)},
                timings=timings,
                started=started,
                retrieval_path=retrieval_path,
                provider=completion.provider,
                model=completion.model,
                prompt_sha=prompt_sha,
                usage=completion.usage,
                prompt_path=prompt_path,
            )

        citation = resolve_citation(
            status="answered",
            registry=self.registry,
            evidence=evidence,
            resolved_scheme=resolution.scheme_id,
        )
        if citation is None:
            return self._not_found_response(
                trace_id=trace_id,
                query=trace_query,
                resolution=resolution,
                evidence=evidence,
                guardrails={"citation": "invalid_evidence"},
                timings=timings,
                started=started,
                retrieval_path=retrieval_path,
                provider=completion.provider,
                model=completion.model,
                prompt_sha=prompt_sha,
                usage=completion.usage,
                prompt_path=prompt_path,
            )

        guardrails = {
            "pii": "pass",
            "intent": intent.intent,
            "resolved_scheme": resolution.scheme_id,
            "resolved_field": resolution.field,
            "sentences": count_sentences(validation.answer),
            "fallback": validation.fallback,
            "repair_attempted": repair_used,
            "filter_fallback": retrieval.filter_fallback,
        }
        self._write_answer_trace(
            trace_id=trace_id,
            status="answered",
            query=trace_query,
            answer=validation.answer,
            citation=citation,
            evidence=evidence,
            guardrails=guardrails,
            stage_timings_ms=timings,
            started=started,
            provider=completion.provider,
            model=completion.model,
            prompt_sha=prompt_sha,
            usage=completion.usage,
            fallback=validation.fallback,
            repair_attempted=repair_used,
            prompt_path=prompt_path,
            retrieval_path=retrieval_path,
        )
        return AnswerResponse(
            trace_id=trace_id,
            status="answered",
            answer=validation.answer,
            citation=citation,
            last_updated=self._last_updated(evidence),
            evidence=evidence,
            guardrails=guardrails,
            stage_timings_ms=timings,
        )

    # ------------------------------------------------------------- subroutines
    def _not_found_response(
        self,
        *,
        trace_id: str,
        query: str | None,
        resolution: Resolution,
        evidence: Sequence[RetrievedEvidence],
        guardrails: dict[str, Any],
        timings: dict[str, float],
        started: float,
        retrieval_path: Path | None = None,
        provider: str = "",
        model: str = "",
        prompt_sha: str = "",
        usage: dict[str, Any] | None = None,
        prompt_path: Path | None = None,
    ) -> AnswerResponse:
        citation = resolve_citation(
            status="not_found",
            registry=self.registry,
            evidence=evidence,
            resolved_scheme=resolution.scheme_id,
            resolved_field=resolution.field,
        )
        self._write_answer_trace(
            trace_id=trace_id,
            status="not_found",
            query=query,
            answer=NOT_FOUND_COPY,
            citation=citation,
            evidence=[],
            guardrails=guardrails,
            stage_timings_ms=timings,
            started=started,
            provider=provider,
            model=model,
            prompt_sha=prompt_sha,
            usage=usage,
            prompt_path=prompt_path,
            retrieval_path=retrieval_path,
        )
        return AnswerResponse(
            trace_id=trace_id,
            status="not_found",
            answer=NOT_FOUND_COPY,
            citation=citation,
            last_updated=self._last_updated(evidence),
            guardrails=guardrails,
            stage_timings_ms=timings,
        )

    def _generation_error(
        self,
        *,
        trace_id: str,
        query: str | None,
        resolution: Resolution,
        evidence: Sequence[RetrievedEvidence],
        guardrails: dict[str, Any],
        timings: dict[str, float],
        started: float,
        prompt_sha: str,
        prompt_path: Path,
        retrieval_path: Path,
        provider: str,
        model: str,
    ) -> AnswerResponse:
        citation = resolve_citation(
            status="error",
            registry=self.registry,
            evidence=evidence,
            resolved_scheme=resolution.scheme_id,
        )
        self._write_answer_trace(
            trace_id=trace_id,
            status="error",
            query=query,
            answer=ERROR_COPY,
            citation=citation,
            evidence=[],
            guardrails=guardrails,
            stage_timings_ms=timings,
            started=started,
            provider=provider,
            model=model,
            prompt_sha=prompt_sha,
            prompt_path=prompt_path,
            retrieval_path=retrieval_path,
        )
        return AnswerResponse(
            trace_id=trace_id,
            status="error",
            answer=ERROR_COPY,
            citation=citation,
            guardrails=guardrails,
            stage_timings_ms=timings,
        )
