"""Startup health check.

Run with ``python -m mf_faq.healthcheck``. Prints OK/WARN/FAIL for config,
provider readiness, embedding model, writable storage, and artifact
consistency. Secret values are never printed, only present/missing.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from mf_faq.config import Settings
from mf_faq.io_utils import count_jsonl, read_json, read_jsonl

OK = "OK"
WARN = "WARN"
FAIL = "FAIL"


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str


@dataclass
class HealthReport:
    checks: list[Check]

    @property
    def ok(self) -> bool:
        return not any(check.status == FAIL for check in self.checks)

    @property
    def warnings(self) -> int:
        return sum(1 for check in self.checks if check.status == WARN)

    def render(self) -> str:
        width = max((len(check.name) for check in self.checks), default=0)
        lines = ["Health check — HDFC MF Facts Assistant"]
        for check in self.checks:
            lines.append(f"  [{check.status}] {check.name.ljust(width)}  {check.detail}")
        result = "OK" if self.ok else "FAIL"
        suffix = f" (warnings: {self.warnings})" if self.warnings else ""
        lines.append(f"Result: {result}{suffix}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "warnings": self.warnings,
            "checks": [
                {"name": check.name, "status": check.status, "detail": check.detail}
                for check in self.checks
            ],
        }


def _writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def _artifact_paths(settings: Settings) -> dict[str, Path]:
    return {
        "documents": settings.resolved_path("data/processed/documents.jsonl"),
        "chunks": settings.resolved_path("data/chunks/chunks.jsonl"),
        "embedding_ids": settings.resolved_path("data/embeddings/embedding_ids.json"),
        "embedding_manifest": settings.resolved_path("data/embeddings/embedding_manifest.json"),
        "index_manifest": settings.resolved_path("data/index_manifest.json"),
    }


def _artifact_check(settings: Settings) -> Check:
    paths = _artifact_paths(settings)
    present = {name: path for name, path in paths.items() if path.is_file()}
    if not present:
        return Check("artifacts", OK, "no artifacts yet (fresh install)")

    missing = [name for name, path in paths.items() if not path.is_file()]
    if missing:
        return Check(
            "artifacts",
            FAIL,
            f"partial artifacts; missing: {', '.join(sorted(missing))}",
        )

    try:
        documents = count_jsonl(paths["documents"])
        chunks = count_jsonl(paths["chunks"])
        indexed = sum(1 for row in read_jsonl(paths["chunks"]) if row.get("indexed") is True)
        embedding_ids = read_json(paths["embedding_ids"])
        embedding_count = len(embedding_ids) if isinstance(embedding_ids, list) else -1
        embedding_manifest = read_json(paths["embedding_manifest"])
        manifest_count = int(embedding_manifest.get("count", -1))
        index_manifest = read_json(paths["index_manifest"])
        index_count = int(index_manifest.get("count", -1))
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        return Check("artifacts", FAIL, f"unreadable artifacts: {exc}")

    problems: list[str] = []
    if documents != 5:
        problems.append(f"documents={documents} (expected 5)")
    if embedding_count != indexed:
        problems.append(f"embedding ids={embedding_count} but indexed chunks={indexed}")
    if manifest_count != embedding_count:
        problems.append(f"embedding manifest count={manifest_count} but ids={embedding_count}")
    if index_count != embedding_count:
        problems.append(f"index count={index_count} but embeddings={embedding_count}")

    if problems:
        return Check("artifacts", FAIL, "; ".join(problems))
    return Check(
        "artifacts",
        OK,
        f"5 documents, {chunks} chunks ({indexed} indexed), {index_count} in Chroma",
    )


def run_healthcheck(
    settings: Settings | None = None,
    *,
    config_path: str | Path | None = None,
    base_dir: str | Path | None = None,
) -> HealthReport:
    if settings is None:
        try:
            settings = Settings.load(config_path, base_dir=base_dir)
        except Exception as exc:  # config failure blocks every later check
            return HealthReport(
                [Check("config", FAIL, f"{type(exc).__name__}: {exc}")]
            )

    checks: list[Check] = [
        Check(
            "config",
            OK,
            f"loaded {settings.config_path.name} (hash {settings.config_hash})",
        )
    ]

    if settings.llm.model:
        checks.append(Check("llm_model", OK, "LLM_MODEL present"))
    else:
        checks.append(Check("llm_model", WARN, "LLM_MODEL missing (set it in .env)"))

    if not settings.llm.provider:
        checks.append(Check("llm_provider", WARN, "LLM_PROVIDER missing (default: groq)"))
    elif settings.provider_key_present():
        checks.append(
            Check(
                "llm_provider",
                OK,
                f"{settings.llm.provider} · {settings.provider_key_env} present",
            )
        )
    else:
        checks.append(
            Check(
                "llm_provider",
                WARN,
                f"{settings.llm.provider} · {settings.provider_key_env} missing",
            )
        )

    checks.append(
        Check(
            "embedding_model",
            OK,
            f"{settings.embedding.model} configured",
        )
    )

    storage_dirs: Iterable[Path] = (
        settings.resolved_path("data/chroma"),
        settings.resolved_path("data/traces"),
        settings.resolved_path("data/embeddings"),
    )
    unwritable = [str(path) for path in storage_dirs if not _writable(path)]
    if unwritable:
        checks.append(Check("storage", FAIL, f"not writable: {', '.join(unwritable)}"))
    else:
        checks.append(Check("storage", OK, "data/chroma, data/traces, data/embeddings writable"))

    checks.append(_artifact_check(settings))
    return HealthReport(checks)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Health check for the HDFC MF facts assistant")
    parser.add_argument("--config", default=None, help="path to config.yaml")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    args = parser.parse_args(argv)

    report = run_healthcheck(config_path=args.config)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(report.render())
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
