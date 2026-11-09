"""ChromaDB persistence, metadata flattening, and the drift guard.

The collection stores one record per indexed chunk. Chroma receives the
pre-computed MiniLM vector, so the collection is created with
``embedding_function=None`` and never re-embeds documents at query time.

See ``implementation.md`` §6.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping, Sequence

import numpy as np

from mf_faq.config import Settings
from mf_faq.contracts import Chunk, EmbeddingManifest, IndexManifest
from mf_faq.embeddings.embedder import EXPECTED_DIMENSION

__all__ = [
    "ChromaStore",
    "IndexDriftError",
    "IndexingError",
    "flatten_metadata_value",
    "metadata_for_chunk",
    "verify_index_drift",
]

DISTANCE_METADATA_KEY = "hnsw:space"


class IndexingError(RuntimeError):
    """The vector index could not be built or read."""


class IndexDriftError(IndexingError):
    """The live collection is incompatible with the embedding artifacts."""


def flatten_metadata_value(value: Any) -> str | int | float | bool:
    """Flatten a metadata value to a Chroma scalar.

    Lists are joined with ``" > "`` and ``None`` becomes ``""``; booleans stay
    booleans so ``where={"indexed": True}`` works as a native filter.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return value
    if isinstance(value, (str, int, float)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return " > ".join(str(flatten_metadata_value(item)) for item in value)
    return str(value)


def metadata_for_chunk(chunk: Chunk) -> dict[str, str | int | float | bool]:
    """Build the flat metadata record described by §6.2."""
    return {
        "doc_id": flatten_metadata_value(chunk.doc_id),
        "url": flatten_metadata_value(chunk.url),
        "title": flatten_metadata_value(chunk.title),
        "scheme_id": flatten_metadata_value(chunk.scheme_id),
        "page_type": flatten_metadata_value(chunk.page_type),
        "section": flatten_metadata_value(chunk.section_path),
        "field": flatten_metadata_value(chunk.field),
        "chunk_type": flatten_metadata_value(chunk.chunk_type),
        "content_class": flatten_metadata_value(chunk.content_class),
        "indexed": flatten_metadata_value(chunk.indexed),
        "fetched_at": flatten_metadata_value(chunk.fetched_at),
    }


def _is_not_found(exc: Exception) -> bool:
    return "notfound" in type(exc).__name__.lower() or "does not exist" in str(exc).lower()


class ChromaStore:
    """A thin, testable wrapper around one persistent Chroma collection."""

    def __init__(self, settings: Settings, *, client: Any | None = None) -> None:
        self.settings = settings
        self.persist_dir = settings.resolved_path(settings.vector_db.persist_dir)
        self.collection_name = settings.vector_db.collection
        self.distance = settings.vector_db.distance
        self.batch_size = settings.vector_db.batch_size
        self._client = client
        self._collection: Any | None = None

    @property
    def client(self) -> Any:
        if self._client is None:
            try:
                import chromadb
            except ImportError as exc:  # pragma: no cover - dependency is declared
                raise IndexingError(
                    "chromadb is not installed; run "
                    '`pip install -e ".[dev]"` or install chromadb'
                ) from exc
            self.persist_dir.mkdir(parents=True, exist_ok=True)
            self._client = chromadb.PersistentClient(path=str(self.persist_dir))
        return self._client

    def _collection_metadata(self) -> dict[str, str | int | float | bool]:
        return {
            DISTANCE_METADATA_KEY: self.distance,
            "model": self.settings.embedding.model,
            "dimension": EXPECTED_DIMENSION,
            "config_hash": self.settings.config_hash,
        }

    def open(self, *, rebuild: bool = False) -> Any:
        """Open (or create) the collection; ``rebuild`` drops it first."""
        if rebuild:
            self.drop()
        self._collection = self.client.get_or_create_collection(
            name=self.collection_name,
            metadata=self._collection_metadata(),
            embedding_function=None,
        )
        return self._collection

    @property
    def collection(self) -> Any:
        if self._collection is None:
            self.open()
        return self._collection

    def drop(self) -> bool:
        """Delete the collection. Returns ``True`` when it existed."""
        try:
            self.client.delete_collection(self.collection_name)
        except Exception as exc:  # Chroma raises a provider-specific not-found error
            self._collection = None
            if _is_not_found(exc):
                return False
            raise IndexingError(
                f"could not delete collection {self.collection_name!r}: {exc}"
            ) from exc
        self._collection = None
        return True

    def upsert(
        self,
        chunks: Sequence[Chunk],
        vectors: np.ndarray,
        *,
        batch_size: int | None = None,
    ) -> int:
        """Upsert chunks and their vectors in configured-size batches."""
        items = list(chunks)
        matrix = np.asarray(vectors, dtype=np.float32)
        if matrix.ndim != 2:
            raise IndexingError(f"embeddings must be 2-D, got shape {matrix.shape}")
        if matrix.shape[0] != len(items):
            raise IndexingError(
                f"got {matrix.shape[0]} vectors for {len(items)} chunks"
            )
        if matrix.shape[1] != EXPECTED_DIMENSION:
            raise IndexingError(
                f"embedding dimension {matrix.shape[1]}; expected {EXPECTED_DIMENSION}"
            )
        if not np.isfinite(matrix).all():
            raise IndexingError("embeddings contain NaN or Inf")
        if any(not chunk.indexed for chunk in items):
            raise IndexingError("only indexed chunks may be written to the collection")
        if not items:
            return 0

        size = batch_size or self.batch_size
        written = 0
        for start in range(0, len(items), size):
            batch = items[start : start + size]
            batch_vectors = np.ascontiguousarray(
                matrix[start : start + size], dtype=np.float32
            )
            self.collection.upsert(
                ids=[chunk.chunk_id for chunk in batch],
                embeddings=batch_vectors,
                documents=[chunk.text for chunk in batch],
                metadatas=[metadata_for_chunk(chunk) for chunk in batch],
            )
            written += len(batch)
        return written

    def count(self) -> int:
        return int(self.collection.count())

    def ids_where(self, where: Mapping[str, Any] | None = None) -> list[str]:
        result = self.collection.get(where=dict(where) if where else None)
        return [str(item) for item in (result.get("ids") or [])]

    def all_ids(self) -> list[str]:
        return self.ids_where()

    def check_drift(
        self,
        embedding_manifest: EmbeddingManifest,
        index_manifest: IndexManifest | None = None,
    ) -> None:
        """Raise ``IndexDriftError`` if the live index is incompatible."""
        live = self.collection.metadata or {}
        problems: list[str] = []

        def compare(label: str, expected: Any, actual: Any) -> None:
            if expected != actual:
                problems.append(f"{label}: found {actual!r}, expected {expected!r}")

        compare("collection model", embedding_manifest.model, live.get("model"))
        compare(
            "collection dimension",
            embedding_manifest.dimension,
            live.get("dimension"),
        )
        compare("collection distance", self.distance, live.get(DISTANCE_METADATA_KEY))
        compare(
            "collection config hash",
            embedding_manifest.config_hash,
            live.get("config_hash"),
        )

        if index_manifest is not None:
            compare("index collection", self.collection_name, index_manifest.collection)
            compare("index model", embedding_manifest.model, index_manifest.model)
            compare(
                "index dimension",
                embedding_manifest.dimension,
                index_manifest.dimension,
            )
            compare("index distance", self.distance, index_manifest.distance)
            compare(
                "index config hash",
                embedding_manifest.config_hash,
                index_manifest.config_hash,
            )
            live_count = self.collection.count()
            if live_count != index_manifest.count:
                problems.append(
                    f"live count: found {live_count}, expected {index_manifest.count}"
                )

        if problems:
            detail = "; ".join(problems)
            raise IndexDriftError(
                f"index drift detected ({detail}); rebuild with "
                "`python -m scripts.run_index --rebuild`"
            )


def verify_index_drift(
    store: ChromaStore,
    embedding_manifest: EmbeddingManifest,
    index_manifest: IndexManifest | None = None,
) -> None:
    """Module-level convenience wrapper for the drift guard."""
    store.check_drift(embedding_manifest, index_manifest)
