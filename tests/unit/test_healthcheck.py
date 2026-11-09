"""Health check behaviour on a fresh and partially built install."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mf_faq.config import PROVIDER_ENV_KEYS, Settings
from mf_faq.healthcheck import FAIL, OK, WARN, run_healthcheck

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "config" / "config.yaml"


@pytest.fixture()
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    for key in PROVIDER_ENV_KEYS.values():
        monkeypatch.delenv(key, raising=False)


def load(tmp_path: Path) -> Settings:
    return Settings.load(CONFIG, base_dir=tmp_path)


def statuses(report) -> dict[str, str]:
    return {check.name: check.status for check in report.checks}


def test_fresh_install_is_healthy_with_warnings(tmp_path: Path, clean_env: None) -> None:
    report = run_healthcheck(load(tmp_path))
    by_name = statuses(report)

    assert report.ok is True
    assert by_name["config"] == OK
    assert by_name["storage"] == OK
    assert by_name["artifacts"] == OK
    assert by_name["llm_model"] == WARN
    assert by_name["llm_provider"] == WARN

    detail = next(check.detail for check in report.checks if check.name == "artifacts")
    assert "fresh install" in detail


def test_configured_provider_is_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("LLM_MODEL", "test-model")
    monkeypatch.setenv("GROQ_API_KEY", "secret-value")

    report = run_healthcheck(load(tmp_path))
    by_name = statuses(report)

    assert report.ok is True
    assert by_name["llm_model"] == OK
    assert by_name["llm_provider"] == OK

    rendered = report.render()
    assert "secret-value" not in rendered
    assert "GROQ_API_KEY present" in rendered


def test_partial_artifacts_fail(tmp_path: Path, clean_env: None) -> None:
    settings = load(tmp_path)
    documents = settings.resolved_path("data/processed/documents.jsonl")
    documents.parent.mkdir(parents=True, exist_ok=True)
    documents.write_text("{}\n", encoding="utf-8")

    report = run_healthcheck(settings)

    assert report.ok is False
    assert statuses(report)["artifacts"] == FAIL
    assert "partial artifacts" in next(
        check.detail for check in report.checks if check.name == "artifacts"
    )


def test_consistent_artifacts_pass(tmp_path: Path, clean_env: None) -> None:
    settings = load(tmp_path)
    data = settings.base_dir / "data"

    documents = [{"doc_id": f"c{index}"} for index in range(1, 6)]
    chunks = [
        {"chunk_id": "a", "indexed": True},
        {"chunk_id": "b", "indexed": True},
        {"chunk_id": "c", "indexed": False},
    ]
    embedding_ids = ["a", "b"]
    embedding_manifest = {"count": 2, "model": "m", "dimension": 384}
    index_manifest = {"count": 2, "collection": "mf_faq_v1"}

    (data / "processed").mkdir(parents=True, exist_ok=True)
    (data / "chunks").mkdir(parents=True, exist_ok=True)
    (data / "embeddings").mkdir(parents=True, exist_ok=True)
    (data / "processed" / "documents.jsonl").write_text(
        "\n".join(json.dumps(row) for row in documents) + "\n", encoding="utf-8"
    )
    (data / "chunks" / "chunks.jsonl").write_text(
        "\n".join(json.dumps(row) for row in chunks) + "\n", encoding="utf-8"
    )
    (data / "embeddings" / "embedding_ids.json").write_text(
        json.dumps(embedding_ids), encoding="utf-8"
    )
    (data / "embeddings" / "embedding_manifest.json").write_text(
        json.dumps(embedding_manifest), encoding="utf-8"
    )
    (data / "index_manifest.json").write_text(json.dumps(index_manifest), encoding="utf-8")

    report = run_healthcheck(settings)

    assert report.ok is True
    assert statuses(report)["artifacts"] == OK
    assert "5 documents" in next(
        check.detail for check in report.checks if check.name == "artifacts"
    )


def test_count_mismatch_fails(tmp_path: Path, clean_env: None) -> None:
    settings = load(tmp_path)
    data = settings.base_dir / "data"
    for directory in ("processed", "chunks", "embeddings"):
        (data / directory).mkdir(parents=True, exist_ok=True)

    (data / "processed" / "documents.jsonl").write_text(
        "".join(json.dumps({"doc_id": f"c{i}"}) + "\n" for i in range(5)), encoding="utf-8"
    )
    (data / "chunks" / "chunks.jsonl").write_text(
        json.dumps({"chunk_id": "a", "indexed": True}) + "\n", encoding="utf-8"
    )
    (data / "embeddings" / "embedding_ids.json").write_text("[]", encoding="utf-8")
    (data / "embeddings" / "embedding_manifest.json").write_text(
        json.dumps({"count": 0}), encoding="utf-8"
    )
    (data / "index_manifest.json").write_text(json.dumps({"count": 0}), encoding="utf-8")

    report = run_healthcheck(settings)

    assert report.ok is False
    assert statuses(report)["artifacts"] == FAIL
