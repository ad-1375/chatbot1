"""Chunk the five Step 1 pages into atomic factual chunks.

Usage::

    python -m scripts.run_chunk

Artifacts:

* ``data/chunks/chunks.jsonl`` — every chunk (indexed and audit-only)
* ``data/chunks/excluded.jsonl`` — non-indexed chunks with reasons
* ``data/chunks/chunk_stats.json`` — machine-readable counts and distributions
* ``data/chunks/chunk_report.md`` — human-readable report and invariants
"""

from __future__ import annotations

import sys

from mf_faq.chunking.pipeline import ChunkingError, run_chunk
from mf_faq.config import Settings


def main() -> int:
    settings = Settings.load()
    try:
        result = run_chunk(settings)
    except ChunkingError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    stats = result.stats
    print(
        f"Chunked {stats['document_count']}/5 pages into {stats['chunk_count']} chunks"
    )
    print(
        f"  indexed {stats['indexed_count']}, excluded {stats['excluded_count']}, "
        f"oversize {stats['oversize_count']}"
    )
    if stats["tokenizer"]["fallback"]:
        print("  tokenizer fallback: word/punctuation counter (MiniLM not installed)")
    for doc_id, page in stats["by_page"].items():
        print(
            f"  - {doc_id}: {page['chunks']} chunks "
            f"({page['indexed']} indexed, {page['excluded']} excluded)"
        )
    print(f"Wrote {result.chunks_path}")
    print(f"Wrote {result.excluded_path}")
    print(f"Wrote {result.stats_path}")
    print(f"Wrote {result.report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
