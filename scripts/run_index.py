"""Upsert the Step 3 embeddings into the persistent Chroma collection.

Usage::

    python -m scripts.run_index
    python -m scripts.run_index --rebuild

Artifacts:

* ``data/chroma/`` — persistent Chroma collection
* ``data/index_manifest.json`` — collection, count, model, distance, config hash
* ``data/index_report.md`` — count, distance, and filter smoke tests
"""

from __future__ import annotations

import argparse
import sys

from mf_faq.config import Settings
from mf_faq.index.pipeline import IndexingError, run_index


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Index the Step 3 embeddings into ChromaDB"
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="drop and recreate the collection before indexing",
    )
    args = parser.parse_args(argv)

    settings = Settings.load()
    try:
        result = run_index(settings, rebuild=args.rebuild)
    except IndexingError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    stats = result.stats
    print(
        f"Indexed {stats['count']} chunks into "
        f"{stats['collection']} ({stats['distance']})"
    )
    print(
        f"  model {stats['model']}, dimension {stats['dimension']}, "
        f"batch size {stats['batch_size']}"
    )
    print(
        f"  rebuilt {stats['rebuild']}, upserted {stats['upserted']} this run"
    )
    for test in stats["filters"]:
        print(
            f"  - {test['filter']}={test['value']!r}: "
            f"{test['returned']}/{test['expected']} records"
        )
    print(f"Wrote {result.manifest_path}")
    print(f"Wrote {result.report_path}")
    print(f"Collection persisted under {result.persist_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
