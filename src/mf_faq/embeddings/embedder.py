"""Encode chunks and queries with MiniLM.

The embedder is intentionally small and injectable: the production path loads
``sentence-transformers/all-MiniLM-L6-v2`` once, while tests can pass a fake
encoder that records calls. Documents are cached by
``sha256(model + content_hash + normalize)`` so duplicate content is encoded
only once. Queries use the same model instance as documents.

See implementation.md §5.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence

import numpy as np

from mf_faq.config import Settings
from mf_faq.contracts import Chunk

EXPECTED_DIMENSION = 384


class EmbeddingError(RuntimeError):
    """Embeddings could not be produced or loaded."""


class EmbeddingInvariantError(EmbeddingError):
    """One or more §5.2 embedding invariants failed."""


class Encoder(Protocol):
    def encode(self, inputs: Sequence[str], **kwargs: Any) -> Any: ...

    def get_sentence_embedding_dimension(self) -> int | None: ...


@dataclass
class EmbeddingBatch:
    vectors: np.ndarray
    chunk_ids: list[str]
    encoded_count: int
    cache_hits: int

    @property
    def count(self) -> int:
        return len(self.chunk_ids)


@dataclass
class EmbeddingCache:
    """In-memory vector cache keyed by model, content hash, and normalisation."""

    vectors: dict[str, np.ndarray] = field(default_factory=dict)

    @staticmethod
    def key(*, model: str, content_hash: str, normalize: bool) -> str:
        payload = f"{model}|{content_hash}|{int(normalize)}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def get(self, key: str) -> np.ndarray | None:
        return self.vectors.get(key)

    def put(self, key: str, vector: np.ndarray) -> None:
        self.vectors[key] = np.asarray(vector, dtype=np.float32)

    def __len__(self) -> int:
        return len(self.vectors)


def load_encoder(settings: Settings) -> Encoder:
    """Load the configured SentenceTransformer once from the local cache."""
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise EmbeddingError(
            "sentence-transformers is not installed; run "
            '`pip install -e ".[dev]"` or install sentence-transformers'
        ) from exc

    device = None if settings.embedding.device == "auto" else settings.embedding.device
    try:
        model = SentenceTransformer(
            settings.embedding.model,
            device=device,
            local_files_only=True,
        )
    except Exception as exc:
        raise EmbeddingError(
            f"could not load embedding model {settings.embedding.model!r} "
            f"from the local Hugging Face cache: {exc}"
        ) from exc

    model.max_seq_length = settings.embedding.max_seq_length
    return model


def _model_dimension(model: Encoder) -> int:
    # sentence-transformers 6.x renamed the getter; support both names.
    for name in ("get_embedding_dimension", "get_sentence_embedding_dimension"):
        getter = getattr(model, name, None)
        if callable(getter):
            dimension = getter()
            if dimension:
                return int(dimension)
    return EXPECTED_DIMENSION


def _as_float32_matrix(value: Any, *, expected_rows: int) -> np.ndarray:
    array = np.asarray(value)
    if array.ndim == 1 and expected_rows == 1:
        array = array.reshape(1, -1)
    if array.ndim != 2:
        raise EmbeddingError(f"encoder returned shape {array.shape}; expected 2-D")
    if array.shape[0] != expected_rows:
        raise EmbeddingError(
            f"encoder returned {array.shape[0]} vectors for {expected_rows} inputs"
        )
    return array.astype(np.float32, copy=False)


class ChunkEmbedder:
    """Encode chunks and queries with one model and a per-run vector cache."""

    def __init__(
        self,
        settings: Settings,
        *,
        model: Encoder | None = None,
        cache: EmbeddingCache | None = None,
    ) -> None:
        self.settings = settings
        self.model_name = settings.embedding.model
        self.normalize = settings.embedding.normalize
        self.model = model or load_encoder(settings)
        self.cache = cache or EmbeddingCache()
        self.dimension = _model_dimension(self.model)
        if self.dimension != EXPECTED_DIMENSION:
            raise EmbeddingError(
                f"model {self.model_name!r} has dimension {self.dimension}; "
                f"expected {EXPECTED_DIMENSION}"
            )

    def _encode(self, texts: Sequence[str]) -> np.ndarray:
        vectors = self.model.encode(
            list(texts),
            batch_size=self.settings.embedding.batch_size,
            normalize_embeddings=self.normalize,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        matrix = _as_float32_matrix(vectors, expected_rows=len(texts))
        if matrix.shape[1] != self.dimension:
            raise EmbeddingError(
                f"encoder returned dimension {matrix.shape[1]}; "
                f"expected {self.dimension}"
            )
        return matrix

    def embed_documents(self, chunks: Sequence[Chunk]) -> EmbeddingBatch:
        """Embed chunks in order, encoding only cache misses."""
        ordered = list(chunks)
        keys = [
            EmbeddingCache.key(
                model=self.model_name,
                content_hash=chunk.content_hash,
                normalize=self.normalize,
            )
            for chunk in ordered
        ]

        missing_keys: list[str] = []
        missing_texts: list[str] = []
        seen: set[str] = set()
        for key, chunk in zip(keys, ordered):
            if key in seen or self.cache.get(key) is not None:
                continue
            seen.add(key)
            missing_keys.append(key)
            missing_texts.append(chunk.text)

        encoded_count = 0
        if missing_texts:
            encoded = self._encode(missing_texts)
            encoded_count = len(missing_texts)
            for key, vector in zip(missing_keys, encoded):
                self.cache.put(key, vector)

        matrix = np.empty((len(ordered), self.dimension), dtype=np.float32)
        for index, key in enumerate(keys):
            vector = self.cache.get(key)
            if vector is None:  # pragma: no cover - defensive
                raise EmbeddingError("internal cache miss after encoding")
            matrix[index] = vector

        return EmbeddingBatch(
            vectors=matrix,
            chunk_ids=[chunk.chunk_id for chunk in ordered],
            encoded_count=encoded_count,
            cache_hits=len(ordered) - encoded_count,
        )

    def embed_query(self, text: str) -> np.ndarray:
        """Embed one query with the same model and normalisation."""
        if not text.strip():
            raise EmbeddingError("query text cannot be blank")
        return self._encode([text])[0]


def validate_embeddings(
    vectors: np.ndarray,
    ids: Sequence[str],
    *,
    expected_dimension: int = EXPECTED_DIMENSION,
    normalized: bool = True,
) -> list[str]:
    """Return human-readable violations of the §5.2 vector invariants."""
    violations: list[str] = []
    array = np.asarray(vectors)

    if array.ndim != 2:
        return [f"vectors must be 2-D, got shape {array.shape}"]
    if array.shape[0] != len(ids):
        violations.append(
            f"vector count {array.shape[0]} does not match id count {len(ids)}"
        )
    if array.shape[1] != expected_dimension:
        violations.append(
            f"dimension {array.shape[1]} does not match expected {expected_dimension}"
        )
    if array.dtype != np.float32:
        violations.append(f"dtype is {array.dtype}; expected float32")
    if not np.isfinite(array).all():
        violations.append("vectors contain NaN or Inf")
    if len(set(ids)) != len(ids):
        violations.append("embedding IDs are not unique")

    if array.size and np.isfinite(array).all():
        norms = np.linalg.norm(array, axis=1)
        if normalized:
            bad = np.abs(norms - 1.0) > 1e-3
            if bad.any():
                violations.append(
                    f"{int(bad.sum())} vectors are not L2-normalised "
                    f"(max error {float(np.abs(norms - 1.0).max()):.2e})"
                )
        if (norms == 0).any():
            violations.append("vectors contain a zero-length row")

    return violations
