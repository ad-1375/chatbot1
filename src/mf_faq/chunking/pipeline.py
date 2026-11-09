"""Chunking orchestration: load Step 1 blocks → atomic chunks → artifacts.

The pipeline is deliberately fail-closed. It validates the input artifacts and
every §4.6 invariant before writing anything, so a partial or inconsistent
chunk set is never left behind for the embedding step.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from mf_faq.chunking.chunker import (
    ChunkingError,
    ChunkInvariantError,
    TokenCounter,
    chunk_document,
    validate_chunks,
)
from mf_faq.config import Settings
from mf_faq.contracts import Chunk, SourcePage
from mf_faq.io_utils import read_jsonl, write_json, write_jsonl, write_text

__all__ = [
    "ChunkingError",
    "ChunkInvariantError",
    "ChunkingResult",
    "run_chunk",
]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class ChunkingResult:
    chunks: list[Chunk]
    excluded: list[Chunk]
    stats: dict[str, Any]
    chunks_path: Path
    excluded_path: Path
    stats_path: Path
    report_path: Path

    @property
    def ok(self) -> bool:
        return bool(self.chunks) and not self.stats["invariant_violations"]


def _expected_doc_ids(settings: Settings) -> list[str]:
    return [seed.id for seed in settings.corpus.seed_pages]


def _validate_documents(documents: list[SourcePage], settings: Settings) -> list[SourcePage]:
    """Require exactly the five configured pages, in seed order."""
    expected = _expected_doc_ids(settings)
    by_id = {document.doc_id: document for document in documents}
    if len(by_id) != len(documents):
        raise ChunkingError("duplicate doc_id in the document set")
    if set(by_id) != set(expected):
        missing = sorted(set(expected) - set(by_id))
        extra = sorted(set(by_id) - set(expected))
        raise ChunkingError(
            f"document set must be exactly the five configured pages "
            f"(missing: {missing or 'none'}; unexpected: {extra or 'none'})"
        )
    return [by_id[doc_id] for doc_id in expected]


def _load_documents(settings: Settings) -> list[SourcePage]:
    path = settings.resolved_path("data/processed/documents.jsonl")
    if not path.is_file():
        raise ChunkingError(
            f"missing {path}; run `python -m scripts.run_ingest` first"
        )
    rows = list(read_jsonl(path))
    if len(rows) != 5:
        raise ChunkingError(
            f"{path} has {len(rows)} documents; Step 1 must produce exactly five"
        )
    return _validate_documents(
        [SourcePage.model_validate(row) for row in rows], settings
    )


def _load_blocks(
    settings: Settings, documents: list[SourcePage]
) -> dict[str, list[dict[str, Any]]]:
    path = settings.resolved_path("data/processed/blocks.jsonl")
    if not path.is_file():
        raise ChunkingError(
            f"missing {path}; run `python -m scripts.run_ingest` first"
        )
    rows = list(read_jsonl(path))
    by_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        doc_id = row.get("doc_id")
        blocks = row.get("blocks")
        if not isinstance(doc_id, str) or not doc_id:
            raise ChunkingError(f"{path}: row without doc_id")
        if doc_id in by_id:
            raise ChunkingError(f"{path}: duplicate doc_id {doc_id}")
        if not isinstance(blocks, list) or not blocks:
            raise ChunkingError(f"{path}: {doc_id} has no blocks")
        by_id[doc_id] = row

    doc_ids = [document.doc_id for document in documents]
    if set(by_id) != set(doc_ids):
        raise ChunkingError(
            f"{path} must cover exactly the five loaded documents "
            f"(missing: {sorted(set(doc_ids) - set(by_id)) or 'none'}; "
            f"unexpected: {sorted(set(by_id) - set(doc_ids)) or 'none'})"
        )

    result: dict[str, list[dict[str, Any]]] = {}
    for document in documents:
        row = by_id[document.doc_id]
        for key, value in (
            ("url", document.url),
            ("title", document.title),
            ("scheme_id", document.scheme_id),
            ("page_type", document.page_type),
        ):
            if row.get(key) not in (None, value):
                raise ChunkingError(
                    f"{path}: {document.doc_id} {key} does not match documents.jsonl"
                )
        result[document.doc_id] = list(row["blocks"])
    return result


def _cross_check(chunks: list[Chunk], documents: list[SourcePage]) -> list[str]:
    """Invariants that need both the chunk set and the page registry."""
    violations: list[str] = []
    by_id = {document.doc_id: document for document in documents}
    seen_doc_ids: set[str] = set()

    for chunk in chunks:
        seen_doc_ids.add(chunk.doc_id)
        source = by_id.get(chunk.doc_id)
        if source is None:
            violations.append(
                f"{chunk.chunk_id}: doc_id {chunk.doc_id} is not one of the five pages"
            )
            continue
        if (
            chunk.url != source.url
            or chunk.title != source.title
            or chunk.scheme_id != source.scheme_id
            or chunk.page_type != source.page_type
        ):
            violations.append(
                f"{chunk.chunk_id}: page metadata does not match {source.doc_id}"
            )
        if not chunk.section_path:
            violations.append(f"{chunk.chunk_id}: empty section_path")
        if not chunk.text.startswith(f"{source.title} >"):
            violations.append(f"{chunk.chunk_id}: missing page/section prefix")

    for doc_id in sorted(set(by_id) - seen_doc_ids):
        violations.append(f"{doc_id}: page produced no chunks")

    return violations


def _build_stats(
    *,
    settings: Settings,
    documents: list[SourcePage],
    chunks: list[Chunk],
    excluded: list[Chunk],
    counter: TokenCounter,
    created_at: datetime,
) -> dict[str, Any]:
    token_counts = [chunk.token_count for chunk in chunks] or [0]
    reasons = Counter(
        chunk.exclusion_reason or "unspecified" for chunk in excluded
    )

    by_page: dict[str, Any] = {}
    for document in documents:
        page_chunks = [chunk for chunk in chunks if chunk.doc_id == document.doc_id]
        by_page[document.doc_id] = {
            "title": document.title,
            "page_type": document.page_type,
            "chunks": len(page_chunks),
            "indexed": sum(1 for chunk in page_chunks if chunk.indexed),
            "excluded": sum(1 for chunk in page_chunks if not chunk.indexed),
            "oversize": sum(1 for chunk in page_chunks if chunk.oversize),
            "tokens": sum(chunk.token_count for chunk in page_chunks),
            "by_content_class": dict(
                sorted(Counter(chunk.content_class for chunk in page_chunks).items())
            ),
            "by_chunk_type": dict(
                sorted(Counter(chunk.chunk_type for chunk in page_chunks).items())
            ),
        }

    return {
        "generated_at": created_at.isoformat(),
        "config_hash": settings.config_hash,
        "document_count": len(documents),
        "chunk_count": len(chunks),
        "indexed_count": sum(1 for chunk in chunks if chunk.indexed),
        "excluded_count": len(excluded),
        "oversize_count": sum(1 for chunk in chunks if chunk.oversize),
        "invariant_violations": 0,
        "max_tokens": settings.chunking.max_tokens,
        "min_tokens": settings.chunking.min_tokens,
        "exclude_content_classes": list(settings.chunking.exclude_content_classes),
        "tokenizer": {
            "model": settings.chunking.tokenizer,
            "fallback": counter.fallback,
        },
        "tokens": {
            "min": min(token_counts),
            "max": max(token_counts),
            "mean": round(sum(token_counts) / len(token_counts), 2),
            "total": sum(token_counts),
        },
        "by_chunk_type": dict(
            sorted(Counter(chunk.chunk_type for chunk in chunks).items())
        ),
        "by_content_class": dict(
            sorted(Counter(chunk.content_class for chunk in chunks).items())
        ),
        "by_field": dict(sorted(Counter(chunk.field for chunk in chunks).items())),
        "exclusion_reasons": dict(sorted(reasons.items())),
        "oversize_chunk_ids": [
            chunk.chunk_id for chunk in chunks if chunk.oversize
        ],
        "by_page": by_page,
    }


def _md_table(headers: list[str], rows: Iterable[Iterable[Any]]) -> list[str]:
    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "---|" * len(headers),
    ]
    for row in rows:
        lines.append("| " + " | ".join(str(cell) for cell in row) + " |")
    return lines


def _render_report(
    *,
    settings: Settings,
    chunks: list[Chunk],
    stats: dict[str, Any],
    created_at: datetime,
) -> str:
    lines: list[str] = [
        "# Chunking report",
        "",
        f"- Generated: {created_at.isoformat()}",
        f"- Config hash: `{settings.config_hash}`",
        f"- Documents: **{stats['document_count']}/5**",
        f"- Chunks: **{stats['chunk_count']}** "
        f"(indexed {stats['indexed_count']}, excluded {stats['excluded_count']})",
        f"- Oversize (not indexed): **{stats['oversize_count']}**",
        f"- Tokenizer: `{stats['tokenizer']['model']}`"
        + (" (heuristic fallback)" if stats["tokenizer"]["fallback"] else ""),
        f"- Token limit: {stats['max_tokens']} max, {stats['min_tokens']} merge threshold",
        "",
        "## Pages",
        "",
    ]
    lines.extend(
        _md_table(
            ["Doc ID", "Chunks", "Indexed", "Excluded", "Oversize", "Tokens"],
            (
                [
                    doc_id,
                    page["chunks"],
                    page["indexed"],
                    page["excluded"],
                    page["oversize"],
                    page["tokens"],
                ]
                for doc_id, page in stats["by_page"].items()
            ),
        )
    )

    lines.extend(["", "## Content classes", ""])
    lines.extend(
        _md_table(
            ["Class", "Chunks"],
            sorted(stats["by_content_class"].items()),
        )
    )

    lines.extend(["", "## Chunk types", ""])
    lines.extend(
        _md_table(["Type", "Chunks"], sorted(stats["by_chunk_type"].items()))
    )

    lines.extend(["", "## Fields", ""])
    lines.extend(_md_table(["Field", "Chunks"], sorted(stats["by_field"].items())))

    lines.extend(["", "## Exclusion reasons", ""])
    lines.extend(
        _md_table(["Reason", "Chunks"], stats["exclusion_reasons"].items())
    )

    lines.extend(["", "## Oversize chunks (kept whole, not indexed)", ""])
    if stats["oversize_chunk_ids"]:
        lines.extend(
            f"- `{chunk.chunk_id}` — {chunk.doc_id} — {chunk.text[:160]}"
            for chunk in chunks
            if chunk.oversize
        )
    else:
        lines.append("- None")

    lines.extend(
        [
            "",
            "## Invariants",
            "",
            "- [x] Every chunk carries doc_id, url, section_path, field, "
            "chunk_type, and content_hash",
            "- [x] No indexed chunk exceeds `max_tokens`",
            "- [x] No key-value pair or table row is split",
            "- [x] Every excluded chunk has a reason",
            "- [x] Chunk IDs are unique within the run",
            "- [x] No chunk crosses a page or scheme boundary",
            "- [x] Every oversize chunk is excluded from the index",
            "",
        ]
    )
    return "\n".join(lines)


def run_chunk(
    settings: Settings | None = None,
    *,
    documents: list[SourcePage] | None = None,
    blocks: Mapping[str, Iterable[Any]] | None = None,
    token_counter: TokenCounter | None = None,
    clock: Any = _utcnow,
) -> ChunkingResult:
    """Run the full chunk step and write artifacts.

    ``documents`` and ``blocks`` let tests inject fixture data without touching
    ``data/processed``. When omitted, both Step 1 artifacts are required.
    """
    settings = settings or Settings.load()
    if (documents is None) != (blocks is None):
        raise ChunkingError(
            "provide both documents and blocks, or neither to load Step 1 artifacts"
        )

    if documents is None:
        documents = _load_documents(settings)
        block_map = _load_blocks(settings, documents)
    else:
        documents = _validate_documents(documents, settings)
        block_map = {doc_id: list(items) for doc_id, items in (blocks or {}).items()}
        if set(block_map) != {document.doc_id for document in documents}:
            raise ChunkingError(
                "injected blocks must cover exactly the five configured documents"
            )

    counter = token_counter or TokenCounter(settings.chunking.tokenizer)
    chunks: list[Chunk] = []
    for document in documents:
        chunks.extend(
            chunk_document(
                document,
                block_map[document.doc_id],
                settings,
                token_counter=counter,
            )
        )

    violations = validate_chunks(chunks, settings=settings, token_counter=counter)
    violations.extend(_cross_check(chunks, documents))
    if violations:
        preview = "\n".join(f"  - {item}" for item in violations[:20])
        raise ChunkInvariantError(
            f"{len(violations)} chunk invariant violation(s):\n{preview}"
        )

    excluded = [chunk for chunk in chunks if not chunk.indexed]
    created_at = clock()
    stats = _build_stats(
        settings=settings,
        documents=documents,
        chunks=chunks,
        excluded=excluded,
        counter=counter,
        created_at=created_at,
    )
    report = _render_report(
        settings=settings,
        chunks=chunks,
        stats=stats,
        created_at=created_at,
    )

    chunks_path = settings.resolved_path("data/chunks/chunks.jsonl")
    excluded_path = settings.resolved_path("data/chunks/excluded.jsonl")
    stats_path = settings.resolved_path("data/chunks/chunk_stats.json")
    report_path = settings.resolved_path("data/chunks/chunk_report.md")

    write_jsonl(chunks_path, chunks)
    write_jsonl(excluded_path, excluded)
    write_json(stats_path, stats)
    write_text(report_path, report)

    return ChunkingResult(
        chunks=chunks,
        excluded=excluded,
        stats=stats,
        chunks_path=chunks_path,
        excluded_path=excluded_path,
        stats_path=stats_path,
        report_path=report_path,
    )
