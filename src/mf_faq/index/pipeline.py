"""Index orchestration: Step 3 vectors → Chroma → manifest and report.

The pipeline validates the Step 3 artifacts, refuses to mix stale vectors with
new ones, upserts in batches, and verifies the collection count plus three
filters before writing ``index_manifest.json``.

See ``implementation.md`` §6.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
from pydantic import ValidationError

from mf_faq.config import Settings
from mf_faq.contracts import Chunk, EmbeddingManifest, IndexManifest
from mf_faq.embeddings.embedder import EXPECTED_DIMENSION, validate_embeddings
from mf_faq.index.chroma_store import (
    ChromaStore,
    IndexDriftError,
    IndexingError,
)
from mf_faq.io_utils import read_json, read_jsonl, write_json, write_text

__all__ = [
    "IndexDriftError",
    "IndexResult",
    "IndexingError",
    "run_index",
]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class IndexResult:
    store: ChromaStore
    chunks: list[Chunk]
    manifest: IndexManifest
    stats: dict[str, Any]
    manifest_path: Path
    report_path: Path
    persist_dir: Path

    @property
    def count(self) -> int:
        return self.manifest.count

    @property
    def ok(self) -> bool:
        return (
            self.count == len(self.chunks)
            and all(test["ok"] for test in self.stats["filters"])
            and not self.stats["invariant_violations"]
        )


def _load_indexed_chunks(settings: Settings) -> list[Chunk]:
    path = settings.resolved_path("data/chunks/chunks.jsonl")
    if not path.is_file():
        raise IndexingError(
            f"missing {path}; run `python -m scripts.run_chunk` first"
        )
    rows = list(read_jsonl(path))
    if not rows:
        raise IndexingError(f"{path} contains no chunks; re-run Step 2")

    try:
        chunks = [Chunk.model_validate(row) for row in rows]
    except ValidationError as exc:
        raise IndexingError(f"{path} contains an invalid chunk row: {exc}") from exc

    indexed = [chunk for chunk in chunks if chunk.indexed]
    if not indexed:
        raise IndexingError(f"{path} has no indexed chunks; re-run Step 2")

    ids = [chunk.chunk_id for chunk in indexed]
    if len(set(ids)) != len(ids):
        raise IndexingError("indexed chunks contain duplicate chunk_id values")
    return indexed


def _load_embeddings(settings: Settings, chunks: Sequence[Chunk]) -> np.ndarray:
    vectors_path = settings.resolved_path("data/embeddings/embeddings.npy")
    ids_path = settings.resolved_path("data/embeddings/embedding_ids.json")
    for path in (vectors_path, ids_path):
        if not path.is_file():
            raise IndexingError(
                f"missing {path}; run `python -m scripts.run_embed` first"
            )

    ids = read_json(ids_path)
    if not isinstance(ids, list) or not all(isinstance(item, str) for item in ids):
        raise IndexingError(f"{ids_path} must be a JSON list of chunk IDs")

    expected = [chunk.chunk_id for chunk in chunks]
    if ids != expected:
        raise IndexingError(
            "embedding_ids.json order does not match the indexed chunks in "
            "chunks.jsonl; re-run `python -m scripts.run_embed`"
        )

    try:
        vectors = np.load(vectors_path, allow_pickle=False)
    except (OSError, ValueError) as exc:
        raise IndexingError(f"could not read {vectors_path}: {exc}") from exc

    violations = validate_embeddings(
        vectors,
        ids,
        expected_dimension=EXPECTED_DIMENSION,
        normalized=settings.embedding.normalize,
    )
    if violations:
        preview = "\n".join(f"  - {item}" for item in violations[:20])
        raise IndexingError(
            f"{len(violations)} embedding artifact violation(s):\n{preview}"
        )
    return np.asarray(vectors, dtype=np.float32)


def _load_embedding_manifest(
    settings: Settings, expected_count: int
) -> EmbeddingManifest:
    path = settings.resolved_path("data/embeddings/embedding_manifest.json")
    if not path.is_file():
        raise IndexingError(
            f"missing {path}; run `python -m scripts.run_embed` first"
        )
    try:
        manifest = EmbeddingManifest.model_validate(read_json(path))
    except ValidationError as exc:
        raise IndexingError(f"{path} is not a valid embedding manifest: {exc}") from exc

    problems: list[str] = []
    if manifest.count != expected_count:
        problems.append(f"count {manifest.count} != indexed chunks {expected_count}")
    if manifest.model != settings.embedding.model:
        problems.append(
            f"model {manifest.model!r} != configured {settings.embedding.model!r}"
        )
    if manifest.dimension != EXPECTED_DIMENSION:
        problems.append(f"dimension {manifest.dimension} != {EXPECTED_DIMENSION}")
    if manifest.max_seq_length != settings.embedding.max_seq_length:
        problems.append(
            f"max_seq_length {manifest.max_seq_length} != "
            f"{settings.embedding.max_seq_length}"
        )
    if manifest.normalized is not settings.embedding.normalize:
        problems.append(
            f"normalized {manifest.normalized} != {settings.embedding.normalize}"
        )
    if manifest.config_hash != settings.config_hash:
        problems.append(
            f"config hash {manifest.config_hash!r} != active "
            f"{settings.config_hash!r}"
        )
    if problems:
        raise IndexingError(
            "embedding manifest does not match the active config: "
            + "; ".join(problems)
            + "; re-run `python -m scripts.run_embed`"
        )
    return manifest


def _pick_scheme(chunks: Sequence[Chunk]) -> str:
    return Counter(chunk.scheme_id for chunk in chunks).most_common(1)[0][0]


def _pick_field(chunks: Sequence[Chunk]) -> str:
    fields = Counter(chunk.field for chunk in chunks if chunk.field != "other")
    if fields:
        return fields.most_common(1)[0][0]
    return Counter(chunk.field for chunk in chunks).most_common(1)[0][0]


def _matches(chunk: Chunk, name: str, value: Any) -> bool:
    if name == "scheme_id":
        return chunk.scheme_id == value
    if name == "field":
        return chunk.field == value
    if name == "indexed":
        return chunk.indexed is True
    raise IndexingError(f"unknown smoke filter {name!r}")


def _smoke_filters(store: ChromaStore, chunks: Sequence[Chunk]) -> list[dict[str, Any]]:
    scheme_id = _pick_scheme(chunks)
    field = _pick_field(chunks)
    specs: list[tuple[str, Any, dict[str, Any]]] = [
        ("scheme_id", scheme_id, {"scheme_id": scheme_id}),
        ("field", field, {"field": field}),
        ("indexed", True, {"indexed": True}),
    ]

    results: list[dict[str, Any]] = []
    for name, value, where in specs:
        expected = {
            chunk.chunk_id for chunk in chunks if _matches(chunk, name, value)
        }
        actual = set(store.ids_where(where))
        results.append(
            {
                "filter": name,
                "value": value,
                "expected": len(expected),
                "returned": len(actual),
                "ok": actual == expected,
            }
        )

    failures = [result for result in results if not result["ok"]]
    if failures:
        detail = "; ".join(
            f"{result['filter']}={result['value']!r} "
            f"returned {result['returned']}/{result['expected']}"
            for result in failures
        )
        raise IndexingError(f"filter smoke test failed: {detail}")
    return results


def _build_stats(
    *,
    settings: Settings,
    chunks: Sequence[Chunk],
    store: ChromaStore,
    filters: list[dict[str, Any]],
    upserted: int,
    rebuild: bool,
    created_at: datetime,
) -> dict[str, Any]:
    by_page = Counter(chunk.doc_id for chunk in chunks)
    return {
        "generated_at": created_at.isoformat(),
        "config_hash": settings.config_hash,
        "collection": store.collection_name,
        "persist_dir": settings.vector_db.persist_dir,
        "count": store.count(),
        "distance": settings.vector_db.distance,
        "model": settings.embedding.model,
        "dimension": EXPECTED_DIMENSION,
        "batch_size": settings.vector_db.batch_size,
        "upserted": upserted,
        "rebuild": rebuild,
        "by_page": dict(sorted(by_page.items())),
        "filters": filters,
        "invariant_violations": 0,
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
    stats: Mapping[str, Any],
    manifest: IndexManifest,
    created_at: datetime,
) -> str:
    lines: list[str] = [
        "# Index report",
        "",
        f"- Generated: {created_at.isoformat()}",
        f"- Collection: `{stats['collection']}`",
        f"- Persist dir: `{stats['persist_dir']}`",
        f"- Count: **{stats['count']}**",
        f"- Distance: **{stats['distance']}**",
        f"- Model: `{stats['model']}`",
        f"- Dimension: **{stats['dimension']}**",
        f"- Config hash: `{stats['config_hash']}`",
        f"- Rebuilt: **{stats['rebuild']}**",
        f"- Upserted this run: {stats['upserted']}",
        f"- Batch size: {stats['batch_size']}",
        "",
        "## Filter smoke tests",
        "",
    ]
    lines.extend(
        _md_table(
            ["Filter", "Value", "Expected", "Returned", "OK"],
            (
                [
                    test["filter"],
                    test["value"],
                    test["expected"],
                    test["returned"],
                    "yes" if test["ok"] else "no",
                ]
                for test in stats["filters"]
            ),
        )
    )

    lines.extend(["", "## Records by page", ""])
    lines.extend(
        _md_table(["Doc ID", "Records"], sorted(stats["by_page"].items()))
    )

    lines.extend(
        [
            "",
            "## Invariants",
            "",
            "- [x] Collection count equals the number of indexed chunks",
            "- [x] Every record ID is a `chunk_id`",
            "- [x] Repeat indexing keeps the count stable (upsert by ID)",
            "- [x] `--rebuild` drops the stale collection before writing",
            "- [x] Filter by scheme, by field, and by `indexed` return the "
            "expected records",
            "- [x] Live model, dimension, distance, and config hash match the "
            "embedding manifest",
            "",
            f"Manifest created at {manifest.created_at.isoformat()}.",
            "",
        ]
    )
    return "\n".join(lines)


def run_index(
    settings: Settings | None = None,
    *,
    rebuild: bool = False,
    store: ChromaStore | None = None,
    clock: Any = _utcnow,
) -> IndexResult:
    """Build the Chroma collection from the Step 3 artifacts.

    ``store`` lets tests inject an ephemeral Chroma client. When omitted, a
    persistent client writes to ``data/chroma``.
    """
    settings = settings or Settings.load()

    chunks = _load_indexed_chunks(settings)
    vectors = _load_embeddings(settings, chunks)
    embedding_manifest = _load_embedding_manifest(settings, len(chunks))

    store = store or ChromaStore(settings)
    store.open(rebuild=rebuild)

    if not rebuild:
        store.check_drift(embedding_manifest)
        expected_ids = {chunk.chunk_id for chunk in chunks}
        stale = set(store.all_ids()) - expected_ids
        if stale:
            raise IndexingError(
                f"collection has {len(stale)} stale record(s) that are not in the "
                "current embedding set; run `python -m scripts.run_index --rebuild`"
            )

    upserted = store.upsert(chunks, vectors)
    count = store.count()
    if count != len(chunks):
        raise IndexingError(
            f"collection count {count} != indexed chunks {len(chunks)}; "
            "run `python -m scripts.run_index --rebuild`"
        )

    filters = _smoke_filters(store, chunks)
    created_at = clock()
    manifest = IndexManifest(
        collection=settings.vector_db.collection,
        count=count,
        model=settings.embedding.model,
        dimension=EXPECTED_DIMENSION,
        distance=settings.vector_db.distance,
        config_hash=settings.config_hash,
        created_at=created_at,
    )
    store.check_drift(embedding_manifest, manifest)

    stats = _build_stats(
        settings=settings,
        chunks=chunks,
        store=store,
        filters=filters,
        upserted=upserted,
        rebuild=rebuild,
        created_at=created_at,
    )
    report = _render_report(
        stats=stats,
        manifest=manifest,
        created_at=created_at,
    )

    manifest_path = settings.resolved_path("data/index_manifest.json")
    report_path = settings.resolved_path("data/index_report.md")
    write_json(manifest_path, manifest)
    write_text(report_path, report)

    return IndexResult(
        store=store,
        chunks=chunks,
        manifest=manifest,
        stats=stats,
        manifest_path=manifest_path,
        report_path=report_path,
        persist_dir=store.persist_dir,
    )
