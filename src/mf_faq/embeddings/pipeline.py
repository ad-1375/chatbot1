"""Embedding orchestration: indexed chunks → MiniLM vectors → artifacts.

The pipeline is fail-closed. It validates the Step 2 artifacts and every
``implementation.md`` §5.2 vector invariant before writing, so a partial or
inconsistent embedding set is never left behind for the Chroma index step.

See ``implementation.md`` §5.
"""

from __future__ import annotations

import os
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np
from pydantic import ValidationError

from mf_faq.config import Settings
from mf_faq.contracts import Chunk, EmbeddingManifest
from mf_faq.embeddings.embedder import (
    EXPECTED_DIMENSION,
    ChunkEmbedder,
    EmbeddingBatch,
    EmbeddingError,
    EmbeddingInvariantError,
    Encoder,
    validate_embeddings,
)
from mf_faq.io_utils import read_jsonl, write_json, write_text

__all__ = [
    "EmbeddingError",
    "EmbeddingInvariantError",
    "EmbeddingResult",
    "run_embed",
]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class EmbeddingResult:
    chunks: list[Chunk]
    vectors: np.ndarray
    ids: list[str]
    manifest: EmbeddingManifest
    stats: dict[str, Any]
    embeddings_path: Path
    ids_path: Path
    manifest_path: Path
    preview_path: Path
    report_path: Path

    @property
    def count(self) -> int:
        return len(self.ids)

    @property
    def ok(self) -> bool:
        return (
            self.count > 0
            and self.vectors.shape == (self.count, EXPECTED_DIMENSION)
            and not self.stats["invariant_violations"]
        )


def _load_indexed_chunks(settings: Settings) -> list[Chunk]:
    """Load ``chunks.jsonl`` and keep only ``indexed: true`` rows, in order."""
    path = settings.resolved_path("data/chunks/chunks.jsonl")
    if not path.is_file():
        raise EmbeddingError(
            f"missing {path}; run `python -m scripts.run_chunk` first"
        )

    rows = list(read_jsonl(path))
    if not rows:
        raise EmbeddingError(f"{path} contains no chunks; re-run Step 2")

    try:
        chunks = [Chunk.model_validate(row) for row in rows]
    except ValidationError as exc:
        raise EmbeddingError(f"{path} contains an invalid chunk row: {exc}") from exc

    indexed = [chunk for chunk in chunks if chunk.indexed]
    if not indexed:
        raise EmbeddingError(
            f"{path} has {len(chunks)} chunks but none are indexed; re-run Step 2"
        )

    ids = [chunk.chunk_id for chunk in indexed]
    if len(set(ids)) != len(ids):
        raise EmbeddingError("indexed chunks contain duplicate chunk_id values")

    return indexed


def _load_chunks(
    settings: Settings, chunks: Sequence[Chunk] | None
) -> list[Chunk]:
    if chunks is None:
        return _load_indexed_chunks(settings)

    ordered = [
        chunk if isinstance(chunk, Chunk) else Chunk.model_validate(chunk)
        for chunk in chunks
    ]
    indexed = [chunk for chunk in ordered if chunk.indexed]
    if not indexed:
        raise EmbeddingError("no indexed chunks to embed")
    return indexed


def _verify_sequence_window(settings: Settings) -> None:
    if settings.embedding.max_seq_length < settings.chunking.max_tokens:
        raise EmbeddingError(
            "embedding.max_seq_length "
            f"({settings.embedding.max_seq_length}) must be >= chunking.max_tokens "
            f"({settings.chunking.max_tokens})"
        )


def _write_npy(path: Path, vectors: np.ndarray) -> Path:
    """Write an ``.npy`` file atomically (sibling temp file + replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    with tmp.open("wb") as handle:
        np.save(handle, vectors, allow_pickle=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    return path


def _build_stats(
    *,
    settings: Settings,
    chunks: list[Chunk],
    vectors: np.ndarray,
    batch: EmbeddingBatch,
    created_at: datetime,
) -> dict[str, Any]:
    norms = np.linalg.norm(vectors, axis=1) if vectors.size else np.array([0.0])
    by_page: dict[str, Any] = {}
    for doc_id in sorted({chunk.doc_id for chunk in chunks}):
        page_chunks = [chunk for chunk in chunks if chunk.doc_id == doc_id]
        by_page[doc_id] = {
            "title": page_chunks[0].title,
            "page_type": page_chunks[0].page_type,
            "chunks": len(page_chunks),
            "content_hashes": len({chunk.content_hash for chunk in page_chunks}),
            "fields": dict(sorted(Counter(chunk.field for chunk in page_chunks).items())),
        }

    return {
        "generated_at": created_at.isoformat(),
        "config_hash": settings.config_hash,
        "model": settings.embedding.model,
        "dimension": EXPECTED_DIMENSION,
        "max_seq_length": settings.embedding.max_seq_length,
        "batch_size": settings.embedding.batch_size,
        "normalized": settings.embedding.normalize,
        "count": len(chunks),
        "encoded_count": batch.encoded_count,
        "cache_hits": batch.cache_hits,
        "unique_content_hashes": len({chunk.content_hash for chunk in chunks}),
        "dtype": str(vectors.dtype),
        "norms": {
            "min": float(norms.min()),
            "max": float(norms.max()),
            "mean": float(norms.mean()),
        },
        "invariant_violations": 0,
        "by_page": by_page,
    }


def _build_preview(
    *,
    settings: Settings,
    chunks: list[Chunk],
    vectors: np.ndarray,
    created_at: datetime,
) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    for index in range(min(3, len(chunks))):
        vector = vectors[index]
        entries.append(
            {
                "chunk_id": chunks[index].chunk_id,
                "doc_id": chunks[index].doc_id,
                "field": chunks[index].field,
                "first_eight_dimensions": [
                    round(float(value), 6) for value in vector[:8]
                ],
                "norm": round(float(np.linalg.norm(vector)), 6),
                "text_preview": chunks[index].text[:200],
            }
        )

    return {
        "model": settings.embedding.model,
        "dimension": EXPECTED_DIMENSION,
        "count": len(chunks),
        "normalized": settings.embedding.normalize,
        "generated_at": created_at.isoformat(),
        "preview": entries,
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
    vectors: np.ndarray,
    stats: dict[str, Any],
    preview: dict[str, Any],
    created_at: datetime,
) -> str:
    norm = stats["norms"]
    lines: list[str] = [
        "# Embedding report",
        "",
        f"- Generated: {created_at.isoformat()}",
        f"- Config hash: `{settings.config_hash}`",
        f"- Model: `{stats['model']}`",
        f"- Dimension: **{stats['dimension']}**",
        f"- Max sequence length: {stats['max_seq_length']}",
        f"- Batch size: {stats['batch_size']}",
        f"- Normalized: **{stats['normalized']}**",
        f"- Indexed chunks: **{stats['count']}**",
        f"- Encoded this run: {stats['encoded_count']} "
        f"(cache hits {stats['cache_hits']})",
        f"- Vectors: `{stats['dtype']}` shape "
        f"({vectors.shape[0]}, {vectors.shape[1]})",
        f"- L2 norm: min {norm['min']:.6f}, max {norm['max']:.6f}, "
        f"mean {norm['mean']:.6f}",
        "",
        "## Pages",
        "",
    ]
    lines.extend(
        _md_table(
            ["Doc ID", "Chunks", "Unique hashes", "Fields"],
            (
                [
                    doc_id,
                    page["chunks"],
                    page["content_hashes"],
                    len(page["fields"]),
                ]
                for doc_id, page in stats["by_page"].items()
            ),
        )
    )

    lines.extend(["", "## Preview (first three vectors)", ""])
    lines.extend(
        _md_table(
            ["Chunk ID", "Norm", "First 8 dimensions", "Text"],
            (
                [
                    f"`{entry['chunk_id']}`",
                    f"{entry['norm']:.6f}",
                    ", ".join(f"{value:.4f}" for value in entry["first_eight_dimensions"]),
                    entry["text_preview"][:90].replace("|", "/"),
                ]
                for entry in preview["preview"]
            ),
        )
    )

    lines.extend(
        [
            "",
            "## Invariants",
            "",
            "- [x] Vector count equals the indexed chunk count",
            "- [x] `embedding_ids.json` row `i` maps to the same chunk as row `i` "
            "of `chunks.jsonl`",
            f"- [x] Every vector has dimension {EXPECTED_DIMENSION}",
            "- [x] Every vector is `float32`",
            "- [x] No vector contains NaN or Inf",
            "- [x] Every L2 norm is within `1e-3` of 1.0",
            "- [x] Embedding IDs are unique",
            "",
        ]
    )
    return "\n".join(lines)


def run_embed(
    settings: Settings | None = None,
    *,
    chunks: Sequence[Chunk] | None = None,
    encoder: Encoder | None = None,
    clock: Any = _utcnow,
) -> EmbeddingResult:
    """Embed indexed chunks in order and write the five Step 3 artifacts.

    ``chunks`` and ``encoder`` let tests inject fixture chunks or a deterministic
    encoder. When omitted, ``data/chunks/chunks.jsonl`` is required and the real
    MiniLM model is loaded once from the local Hugging Face cache.
    """
    settings = settings or Settings.load()
    _verify_sequence_window(settings)

    ordered = _load_chunks(settings, chunks)
    embedder = ChunkEmbedder(settings, model=encoder)
    batch = embedder.embed_documents(ordered)
    vectors = np.ascontiguousarray(batch.vectors, dtype=np.float32)
    ids = list(batch.chunk_ids)

    violations = validate_embeddings(
        vectors,
        ids,
        expected_dimension=EXPECTED_DIMENSION,
        normalized=settings.embedding.normalize,
    )
    if violations:
        preview = "\n".join(f"  - {item}" for item in violations[:20])
        raise EmbeddingInvariantError(
            f"{len(violations)} embedding invariant violation(s):\n{preview}"
        )

    created_at = clock()
    stats = _build_stats(
        settings=settings,
        chunks=ordered,
        vectors=vectors,
        batch=batch,
        created_at=created_at,
    )
    preview = _build_preview(
        settings=settings,
        chunks=ordered,
        vectors=vectors,
        created_at=created_at,
    )
    manifest = EmbeddingManifest(
        model=settings.embedding.model,
        dimension=EXPECTED_DIMENSION,
        max_seq_length=settings.embedding.max_seq_length,
        count=len(ids),
        normalized=settings.embedding.normalize,
        config_hash=settings.config_hash,
        created_at=created_at,
    )
    report = _render_report(
        settings=settings,
        chunks=ordered,
        vectors=vectors,
        stats=stats,
        preview=preview,
        created_at=created_at,
    )

    embeddings_path = settings.resolved_path("data/embeddings/embeddings.npy")
    ids_path = settings.resolved_path("data/embeddings/embedding_ids.json")
    manifest_path = settings.resolved_path("data/embeddings/embedding_manifest.json")
    preview_path = settings.resolved_path("data/embeddings/embedding_preview.json")
    report_path = settings.resolved_path("data/embeddings/embed_report.md")

    _write_npy(embeddings_path, vectors)
    write_json(ids_path, ids)
    write_json(manifest_path, manifest)
    write_json(preview_path, preview)
    write_text(report_path, report)

    return EmbeddingResult(
        chunks=ordered,
        vectors=vectors,
        ids=ids,
        manifest=manifest,
        stats=stats,
        embeddings_path=embeddings_path,
        ids_path=ids_path,
        manifest_path=manifest_path,
        preview_path=preview_path,
        report_path=report_path,
    )
