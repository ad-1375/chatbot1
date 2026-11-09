"""Atomic JSON/JSONL artifact helpers.

Every artifact write goes to a sibling ``.tmp`` file, is flushed and fsynced,
then renamed over the target. A crash never leaves a half-written artifact.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable, Iterator

from pydantic import BaseModel


def _serialize(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return value


def dumps(value: Any) -> str:
    return json.dumps(_serialize(value), ensure_ascii=False, default=str)


def _atomic_write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    return path


def write_json(path: str | Path, value: Any) -> Path:
    """Write one JSON document atomically."""
    return _atomic_write(Path(path), json.dumps(_serialize(value), ensure_ascii=False, indent=2, default=str) + "\n")


def write_jsonl(path: str | Path, rows: Iterable[Any]) -> Path:
    """Write a JSONL file atomically; each row must be JSON-serializable."""
    lines = [dumps(row) for row in rows]
    text = "\n".join(lines) + ("\n" if lines else "")
    return _atomic_write(Path(path), text)


def write_text(path: str | Path, text: str) -> Path:
    """Write plain text atomically (used for raw HTML snapshots and reports)."""
    return _atomic_write(Path(path), text)


def read_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def read_jsonl(path: str | Path) -> Iterator[dict[str, Any]]:
    with Path(path).open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:  # pragma: no cover - defensive
                raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc


def count_jsonl(path: str | Path) -> int:
    return sum(1 for _ in read_jsonl(path))
