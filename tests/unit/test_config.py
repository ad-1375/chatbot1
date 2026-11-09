"""Config loading and validation tests."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from mf_faq.config import PROVIDER_ENV_KEYS, SUPPORTED_PROVIDERS, Settings

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "config" / "config.yaml"


@pytest.fixture()
def raw_config() -> dict:
    return yaml.safe_load(CONFIG.read_text(encoding="utf-8"))


@pytest.fixture()
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    for key in PROVIDER_ENV_KEYS.values():
        monkeypatch.delenv(key, raising=False)


def load_variant(tmp_path: Path, data: dict, **kwargs) -> Settings:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return Settings.load(path, base_dir=tmp_path, **kwargs)


def test_repo_config_loads_without_env(clean_env: None) -> None:
    settings = Settings.load(CONFIG)

    assert len(settings.corpus.seed_pages) == 5
    assert settings.llm.provider == ""
    assert settings.llm.model == ""
    assert settings.config_hash
    assert settings.config_hash == Settings.load(CONFIG).config_hash


def test_seed_ids_and_schemes_are_canonical(clean_env: None) -> None:
    settings = Settings.load(CONFIG)
    pages = settings.corpus.seed_pages

    assert [page.id for page in pages] == [
        "c1_amc_hdfc",
        "c2_hdfc_large_cap",
        "c3_hdfc_flexi_cap",
        "c4_hdfc_elss",
        "c5_hdfc_mid_cap",
    ]
    assert [page.scheme_id for page in pages] == [
        "amc_hdfc",
        "hdfc_large_cap",
        "hdfc_flexi_cap",
        "hdfc_elss",
        "hdfc_mid_cap",
    ]
    assert all(page.url.startswith("https://groww.in/") for page in pages)


def test_env_placeholders_are_expanded(
    tmp_path: Path, raw_config: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("LLM_MODEL", "llama-3.3-70b-versatile")

    settings = load_variant(tmp_path, raw_config)

    assert settings.llm.provider == "groq"
    assert settings.llm.model == "llama-3.3-70b-versatile"


def test_all_supported_providers_accepted(
    tmp_path: Path, raw_config: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    for provider in SUPPORTED_PROVIDERS:
        monkeypatch.setenv("LLM_PROVIDER", provider)
        monkeypatch.setenv("LLM_MODEL", "test-model")
        settings = load_variant(tmp_path, copy.deepcopy(raw_config))
        assert settings.llm.provider == provider


def test_require_llm_reports_missing_configuration(
    tmp_path: Path, raw_config: dict, monkeypatch: pytest.MonkeyPatch, clean_env: None
) -> None:
    with pytest.raises(ValueError, match="LLM_PROVIDER"):
        load_variant(tmp_path, raw_config, require_llm=True)

    monkeypatch.setenv("LLM_PROVIDER", "groq")
    with pytest.raises(ValueError, match="LLM_MODEL"):
        load_variant(tmp_path, raw_config, require_llm=True)


def test_require_llm_reports_missing_key(
    tmp_path: Path, raw_config: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("LLM_MODEL", "test-model")

    with pytest.raises(ValueError, match="GROQ_API_KEY"):
        load_variant(tmp_path, raw_config, require_llm=True)

    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    settings = load_variant(tmp_path, raw_config, require_llm=True)
    assert settings.provider_key_env == "GROQ_API_KEY"
    assert settings.provider_key_present() is True


def test_exactly_five_seeds_required(tmp_path: Path, raw_config: dict, clean_env: None) -> None:
    raw_config["corpus"]["seed_pages"] = raw_config["corpus"]["seed_pages"][:4]
    with pytest.raises(ValidationError, match="exactly five"):
        load_variant(tmp_path, raw_config)


def test_duplicate_seed_ids_rejected(tmp_path: Path, raw_config: dict, clean_env: None) -> None:
    raw_config["corpus"]["seed_pages"][1]["id"] = raw_config["corpus"]["seed_pages"][0]["id"]
    with pytest.raises(ValidationError, match="unique"):
        load_variant(tmp_path, raw_config)


def test_off_allowlist_seed_rejected(tmp_path: Path, raw_config: dict, clean_env: None) -> None:
    raw_config["corpus"]["seed_pages"][0]["url"] = "https://example.com/hdfc"
    with pytest.raises(ValidationError, match="allowlist"):
        load_variant(tmp_path, raw_config)


def test_link_following_disabled(tmp_path: Path, raw_config: dict, clean_env: None) -> None:
    raw_config["corpus"]["follow_links"] = True
    with pytest.raises(ValidationError):
        load_variant(tmp_path, raw_config)


def test_final_k_cannot_exceed_top_k(tmp_path: Path, raw_config: dict, clean_env: None) -> None:
    raw_config["retrieval"]["final_k"] = raw_config["retrieval"]["top_k"] + 1
    with pytest.raises(ValidationError, match="final_k"):
        load_variant(tmp_path, raw_config)


def test_sentence_limits_must_match(tmp_path: Path, raw_config: dict, clean_env: None) -> None:
    raw_config["guardrails"]["max_sentences"] = 2
    with pytest.raises(ValidationError, match="max_sentences"):
        load_variant(tmp_path, raw_config)


def test_embedding_window_must_cover_chunks(tmp_path: Path, raw_config: dict, clean_env: None) -> None:
    raw_config["embedding"]["max_seq_length"] = 100
    with pytest.raises(ValidationError, match="max_seq_length"):
        load_variant(tmp_path, raw_config)


def test_unsupported_provider_rejected(tmp_path: Path, raw_config: dict, clean_env: None) -> None:
    raw_config["llm"]["provider"] = "cohere"
    with pytest.raises(ValidationError, match="unsupported LLM provider"):
        load_variant(tmp_path, raw_config)


def test_min_tokens_cannot_exceed_max(tmp_path: Path, raw_config: dict, clean_env: None) -> None:
    raw_config["chunking"]["min_tokens"] = raw_config["chunking"]["max_tokens"] + 1
    with pytest.raises(ValidationError, match="min_tokens"):
        load_variant(tmp_path, raw_config)


def test_config_hash_changes_with_content(tmp_path: Path, raw_config: dict, clean_env: None) -> None:
    baseline = load_variant(tmp_path, copy.deepcopy(raw_config))
    raw_config["app"]["title"] = "Different title"
    changed = load_variant(tmp_path, raw_config)

    assert baseline.config_hash != changed.config_hash


def test_resolved_path_uses_base_dir(tmp_path: Path, raw_config: dict, clean_env: None) -> None:
    settings = load_variant(tmp_path, raw_config)
    assert settings.resolved_path("data/chroma") == tmp_path / "data" / "chroma"
