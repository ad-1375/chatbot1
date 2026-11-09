"""Ingestion orchestration: fetch → extract → write audit artifacts."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from mf_faq.config import SeedPage, Settings
from mf_faq.contracts import SourcePage
from mf_faq.ingest.extract import ExtractedDocument, extract_document
from mf_faq.ingest.loader import (
    FetchedPage,
    LoadFailure,
    PageLoader,
    _utcnow,
)
from mf_faq.io_utils import write_json, write_jsonl, write_text

COVERAGE_LABELS: dict[str, str] = {
    "expense_ratio": "Expense ratio",
    "exit_load": "Exit load",
    "minimum_sip": "Minimum SIP",
    "lock_in": "Lock-in",
    "riskometer": "Riskometer",
    "benchmark": "Benchmark",
    "statement_download": "Statement download",
}


class IngestError(RuntimeError):
    """Fewer than five pages could be fetched and extracted."""

    def __init__(self, message: str, failures: list[LoadFailure] | None = None) -> None:
        super().__init__(message)
        self.failures = failures or []


@dataclass
class IngestedDocument:
    source: SourcePage
    extracted: ExtractedDocument
    seed: SeedPage
    raw_path: Path | None
    warnings: list[str] = field(default_factory=list)


@dataclass
class IngestResult:
    documents: list[IngestedDocument]
    failures: list[LoadFailure]
    manifest: dict[str, Any]
    manifest_path: Path
    report_path: Path
    blocks_path: Path

    @property
    def ok(self) -> bool:
        return not self.failures and len(self.documents) == 5


def _content_hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_raw_snapshot(settings: Settings, page: FetchedPage) -> Path:
    stamp = page.fetched_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = settings.resolved_path(f"data/raw/{page.seed.id}/{stamp}.html")
    write_text(path, page.html)
    return path


def _source_page(page: FetchedPage, extracted: ExtractedDocument) -> SourcePage:
    return SourcePage(
        doc_id=page.seed.id,
        url=page.final_url,
        title=extracted.title,
        page_type=page.seed.page_type,
        scheme_id=page.seed.scheme_id,
        fetched_at=page.fetched_at,
        http_status=page.status_code,
        content_hash=_content_hash(extracted.text),
        text=extracted.text,
    )


def _safe_relative(path: Path | None, settings: Settings) -> str:
    if path is None:
        return ""
    try:
        return path.resolve().relative_to(settings.base_dir.resolve()).as_posix()
    except ValueError:
        return str(path)


def _manifest_document(
    item: IngestedDocument,
    settings: Settings,
) -> dict[str, Any]:
    source = item.source
    return {
        "doc_id": source.doc_id,
        "seed_url": item.seed.url,
        "url": source.url,
        "title": source.title,
        "page_type": source.page_type,
        "scheme_id": source.scheme_id,
        "fetched_at": source.fetched_at.isoformat(),
        "http_status": source.http_status,
        "content_hash": source.content_hash,
        "renderer": item.extracted.renderer,
        "word_count": item.extracted.word_count,
        "coverage": item.extracted.coverage,
        "warnings": item.warnings,
        "raw_path": _safe_relative(item.raw_path, settings),
    }


def _render_report(
    settings: Settings,
    documents: list[IngestedDocument],
    failures: list[LoadFailure],
    created_at: datetime,
) -> str:
    lines: list[str] = [
        "# Ingestion load report",
        "",
        f"- Generated: {created_at.isoformat()}",
        f"- Config hash: `{settings.config_hash}`",
        f"- Pages loaded: **{len(documents)}/5**",
        f"- Failures: **{len(failures)}**",
        "",
        "## Pages",
        "",
        "| Doc ID | Status | Renderer | Fetched at | Words | Hash | Title |",
        "|---|---|---|---|---|---|---|",
    ]
    for item in documents:
        source = item.source
        lines.append(
            "| {doc} | OK | {renderer} | {fetched} | {words} | `{hash}` | {title} |".format(
                doc=source.doc_id,
                renderer=item.extracted.renderer,
                fetched=source.fetched_at.astimezone(timezone.utc).strftime(
                    "%Y-%m-%d %H:%M UTC"
                ),
                words=item.extracted.word_count,
                hash=source.content_hash[:19],
                title=source.title.replace("|", "/"),
            )
        )

    lines.extend(
        [
            "",
            "## Fact coverage",
            "",
            "Missing facts are reported here and are never invented downstream.",
            "",
            "| Doc ID | "
            + " | ".join(COVERAGE_LABELS.values())
            + " |",
            "|---|" + "---|" * len(COVERAGE_LABELS),
        ]
    )
    for item in documents:
        cells = [
            "yes" if item.extracted.coverage.get(fact) else "-"
            for fact in COVERAGE_LABELS
        ]
        lines.append(f"| {item.source.doc_id} | " + " | ".join(cells) + " |")

    lines.extend(["", "## Source URLs", ""])
    for item in documents:
        lines.append(f"- `{item.source.doc_id}` — {item.source.url}")

    warnings = [
        f"`{item.source.doc_id}`: {warning}"
        for item in documents
        for warning in item.warnings
    ]
    lines.extend(["", "## Warnings", ""])
    lines.extend([f"- {warning}" for warning in warnings] or ["- None"])

    if failures:
        lines.extend(["", "## Failures", ""])
        for failure in failures:
            lines.append(
                f"- `{failure.seed.id}` ({failure.error_type}): {failure.error}"
            )

    return "\n".join(lines) + "\n"


def _collect(
    settings: Settings,
    pages: Iterable[FetchedPage],
    initial_failures: list[LoadFailure],
) -> tuple[list[IngestedDocument], list[LoadFailure]]:
    documents: list[IngestedDocument] = []
    failures = list(initial_failures)

    for page in pages:
        try:
            extracted = extract_document(
                page.html,
                fallback_title=page.seed.id,
                renderer=page.renderer,
            )
            raw_path = page.raw_path or _write_raw_snapshot(settings, page)
            documents.append(
                IngestedDocument(
                    source=_source_page(page, extracted),
                    extracted=extracted,
                    seed=page.seed,
                    raw_path=raw_path,
                    warnings=[*page.warnings, *extracted.warnings],
                )
            )
        except Exception as exc:  # keep processing so every failure is reported
            failures.append(
                LoadFailure(
                    seed=page.seed,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
            )

    documents.sort(key=lambda item: item.seed.id)
    return documents, failures


def run_ingest(
    settings: Settings | None = None,
    *,
    pages: list[FetchedPage] | None = None,
    loader: PageLoader | None = None,
    clock: Any = _utcnow,
) -> IngestResult:
    """Run the full load step and write artifacts.

    ``pages`` lets tests inject already-fetched fixtures without touching the
    network. When omitted, ``loader`` (or a fresh ``PageLoader``) performs the
    five fetches.
    """
    settings = settings or Settings.load()
    owned_loader = False
    failures: list[LoadFailure] = []

    if pages is None:
        if loader is None:
            loader = PageLoader(settings)
            owned_loader = True
        try:
            batch = loader.fetch_all()
            pages = batch.pages
            failures.extend(batch.failures)
        finally:
            if owned_loader and loader is not None:
                loader.close()

    documents, failures = _collect(settings, pages or [], failures)

    if failures or len(documents) != 5:
        detail = "; ".join(
            f"{failure.seed.id}: {failure.error_type}: {failure.error}"
            for failure in failures
        )
        raise IngestError(
            f"ingestion incomplete: {len(documents)}/5 pages loaded"
            + (f" ({detail})" if detail else ""),
            failures=failures,
        )

    created_at = clock()
    manifest = {
        "created_at": created_at.isoformat(),
        "config_hash": settings.config_hash,
        "document_count": len(documents),
        "failure_count": 0,
        "documents": [_manifest_document(item, settings) for item in documents],
        "coverage": {
            item.source.doc_id: item.extracted.coverage for item in documents
        },
        "warnings": [
            f"{item.source.doc_id}: {warning}"
            for item in documents
            for warning in item.warnings
        ],
    }

    documents_path = settings.resolved_path("data/processed/documents.jsonl")
    blocks_path = settings.resolved_path("data/processed/blocks.jsonl")
    report_path = settings.resolved_path("data/processed/load_report.md")
    manifest_path = settings.resolved_path("data/manifests/load_manifest.json")

    write_jsonl(documents_path, [item.source for item in documents])
    write_jsonl(
        blocks_path,
        (
            {
                "doc_id": item.source.doc_id,
                "title": item.source.title,
                "url": item.source.url,
                "scheme_id": item.source.scheme_id,
                "page_type": item.source.page_type,
                "fetched_at": item.source.fetched_at.isoformat(),
                "content_hash": item.source.content_hash,
                "blocks": [block.to_dict() for block in item.extracted.blocks],
            }
            for item in documents
        ),
    )
    write_text(report_path, _render_report(settings, documents, [], created_at))
    write_json(manifest_path, manifest)

    return IngestResult(
        documents=documents,
        failures=[],
        manifest=manifest,
        manifest_path=manifest_path,
        report_path=report_path,
        blocks_path=blocks_path,
    )
