"""Embed the indexed Step 2 chunks with MiniLM.

Usage::

    python -m scripts.run_embed

Artifacts:

* ``data/embeddings/embeddings.npy`` — ``(N, 384)`` float32 L2-normalized rows
* ``data/embeddings/embedding_ids.json`` — row ``i`` maps to chunk ``i``
* ``data/embeddings/embedding_manifest.json`` — model/config/count manifest
* ``data/embeddings/embedding_preview.json`` — first three vectors and norms
* ``data/embeddings/embed_report.md`` — human-readable report and invariants
"""

from __future__ import annotations

import sys

from mf_faq.config import Settings
from mf_faq.embeddings.pipeline import EmbeddingError, run_embed


def main() -> int:
    settings = Settings.load()
    try:
        result = run_embed(settings)
    except EmbeddingError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    stats = result.stats
    print(f"Embedded {stats['count']} indexed chunks with {stats['model']}")
    print(
        f"  dimension {stats['dimension']}, dtype {stats['dtype']}, "
        f"normalized {stats['normalized']}"
    )
    print(
        f"  encoded {stats['encoded_count']} unique vectors, "
        f"{stats['cache_hits']} cache/duplicate hits"
    )
    print(
        f"  L2 norms: min {stats['norms']['min']:.6f}, "
        f"max {stats['norms']['max']:.6f}"
    )
    for doc_id, page in stats["by_page"].items():
        print(f"  - {doc_id}: {page['chunks']} vectors")
    print(f"Wrote {result.embeddings_path}")
    print(f"Wrote {result.ids_path}")
    print(f"Wrote {result.manifest_path}")
    print(f"Wrote {result.preview_path}")
    print(f"Wrote {result.report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
