"""Ask one question through the Step 5 answer service.

Usage::

    python -m scripts.run_answer "What is the expense ratio of HDFC ELSS?"
    python -m scripts.run_answer "What about the exit load?" --last-scheme hdfc_elss --turn-count 1
    python -m scripts.run_answer "Should I buy HDFC ELSS?" --json

The command requires the active provider key in ``.env``. Every request writes
an answer trace under ``data/traces/answers`` and, when retrieval runs, a
retrieval trace under ``data/traces/retrieval``.
"""

from __future__ import annotations

import argparse
import json
import sys

from mf_faq.config import Settings
from mf_faq.contracts import QueryRequest
from mf_faq.generation.answer import AnswerService


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Ask one facts-only question against the Step 5 pipeline"
    )
    parser.add_argument("query", nargs="+", help="the question to ask")
    parser.add_argument(
        "--session-id",
        default="cli",
        help="session identifier for traces (default: cli)",
    )
    parser.add_argument(
        "--last-scheme",
        default=None,
        help="previous resolved scheme for a scheme-less follow-up",
    )
    parser.add_argument(
        "--turn-count",
        type=int,
        default=0,
        help="number of turns since the scheme was last resolved (max 3)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the full AnswerResponse JSON instead of a summary",
    )
    args = parser.parse_args(argv)

    try:
        settings = Settings.load(require_llm=True)
    except (FileNotFoundError, ValueError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    question = " ".join(args.query).strip()
    service = AnswerService(settings)
    response = service.answer(
        QueryRequest(
            query=question,
            session_id=args.session_id,
            last_scheme=args.last_scheme,
            turn_count=max(0, args.turn_count),
        )
    )

    if args.json:
        print(json.dumps(response.model_dump(mode="json"), indent=2, ensure_ascii=False))
    else:
        print(f"Status: {response.status}")
        print(response.answer)
        if response.citation is not None:
            print(f"Source: {response.citation.title}")
            print(f"        {response.citation.url}")
        if response.last_updated:
            print(f"Last updated from sources: {response.last_updated}")
        print(f"Trace: {response.trace_id}")

    traces = settings.resolved_path("data/traces")
    print(f"Answer trace: {traces / 'answers' / f'{response.trace_id}.json'}")
    return 0 if response.status != "error" else 1


if __name__ == "__main__":
    raise SystemExit(main())
