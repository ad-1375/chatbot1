"""Scrape the five seed pages and write the Step 1 audit artifacts.

Usage::

    python -m scripts.run_ingest

Artifacts:

* ``data/raw/{doc_id}/{timestamp}.html`` — raw snapshots
* ``data/processed/documents.jsonl`` — five validated ``SourcePage`` rows
* ``data/processed/blocks.jsonl`` — atomic blocks for the chunker
* ``data/processed/load_report.md`` — status, renderer, hash, fact coverage
* ``data/manifests/load_manifest.json`` — machine-readable load manifest
"""

from __future__ import annotations

import sys

from mf_faq.config import Settings
from mf_faq.ingest.pipeline import IngestError, run_ingest


def main() -> int:
    settings = Settings.load()
    try:
        result = run_ingest(settings)
    except IngestError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        for failure in exc.failures:
            print(
                f"  - {failure.seed.id}: {failure.error_type}: {failure.error}",
                file=sys.stderr,
            )
        return 1

    print(f"Loaded {len(result.documents)}/5 pages")
    for item in result.documents:
        missing = [fact for fact, found in item.extracted.coverage.items() if not found]
        suffix = f"; missing facts: {', '.join(missing)}" if missing else ""
        print(
            f"  - {item.source.doc_id}: {item.extracted.word_count} words, "
            f"hash {item.source.content_hash[:19]}{suffix}"
        )
    print(f"Wrote {result.manifest_path}")
    print(f"Wrote {result.report_path}")
    print(f"Wrote {result.blocks_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
