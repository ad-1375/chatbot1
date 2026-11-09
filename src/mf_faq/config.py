"""Typed settings for the HDFC MF facts assistant.

Loads ``config/config.yaml``, expands ``${ENV_VAR}`` placeholders from the
environment (optionally seeded from ``.env``), validates the result, and
exposes a stable config hash for drift detection.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"

SUPPORTED_PROVIDERS: tuple[str, ...] = (
    "groq",
    "gemini",
    "openai",
    "anthropic",
    "mistral",
)

PROVIDER_ENV_KEYS: dict[str, str] = {
    "groq": "GROQ_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "mistral": "MISTRAL_API_KEY",
}

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _load_dotenv(path: Path) -> None:
    """Load a .env file if python-dotenv is available. Never override real env."""
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - dependency is declared
        return
    if path.is_file():
        load_dotenv(dotenv_path=path, override=False)


def _expand_env(value: Any) -> Any:
    """Recursively replace ${VAR} placeholders with environment values."""
    if isinstance(value, str):
        return _ENV_PATTERN.sub(lambda match: os.environ.get(match.group(1), ""), value)
    if isinstance(value, dict):
        return {key: _expand_env(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand_env(item) for item in value]
    return value


def _stable_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def _host_allowed(host: str, allowlist: list[str]) -> bool:
    host = host.lower().strip(".")
    return any(host == domain or host.endswith(f".{domain}") for domain in allowlist)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AppConfig(_StrictModel):
    title: str = Field(min_length=1)
    max_answer_sentences: int = Field(default=3, ge=1, le=3)
    max_query_chars: int = Field(default=500, ge=50)
    disclaimer: str = Field(min_length=1)


class SeedPage(_StrictModel):
    id: str = Field(min_length=1, pattern=r"^[a-z0-9_]+$")
    url: str = Field(min_length=1)
    page_type: Literal["amc", "scheme"]
    scheme_id: str = Field(min_length=1, pattern=r"^[a-z0-9_]+$")

    @field_validator("url")
    @classmethod
    def _http_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError(f"seed URL must be absolute http(s): {value!r}")
        return value


class CorpusConfig(_StrictModel):
    follow_links: Literal[False] = False
    max_depth: Literal[0] = 0
    allowlist_domains: list[str] = Field(min_length=1)
    request_delay_seconds: float = Field(default=2.0, ge=0)
    timeout_seconds: float = Field(default=30.0, gt=0)
    max_retries: int = Field(default=3, ge=0)
    max_page_bytes: int = Field(default=5 * 1024 * 1024, gt=0)
    render_js_fallback: bool = True
    user_agent: str = Field(min_length=1)
    seed_pages: list[SeedPage] = Field(min_length=1)

    @field_validator("allowlist_domains")
    @classmethod
    def _normalize_domains(cls, value: list[str]) -> list[str]:
        normalized = [domain.strip().lower().strip(".") for domain in value]
        if any(not domain for domain in normalized):
            raise ValueError("allowlist_domains cannot contain empty values")
        return normalized

    @model_validator(mode="after")
    def _validate_corpus(self) -> "CorpusConfig":
        if len(self.seed_pages) != 5:
            raise ValueError(f"exactly five seed pages are required, got {len(self.seed_pages)}")

        ids = [page.id for page in self.seed_pages]
        if len(set(ids)) != len(ids):
            raise ValueError("seed page ids must be unique")
        scheme_ids = [page.scheme_id for page in self.seed_pages]
        if len(set(scheme_ids)) != len(scheme_ids):
            raise ValueError("seed scheme ids must be unique")

        for page in self.seed_pages:
            host = (urlparse(page.url).hostname or "").lower()
            if not _host_allowed(host, self.allowlist_domains):
                raise ValueError(
                    f"seed {page.id} host {host!r} is not on the allowlist "
                    f"{self.allowlist_domains}"
                )
        return self


class ChunkingConfig(_StrictModel):
    max_tokens: int = Field(default=220, gt=0)
    min_tokens: int = Field(default=40, ge=0)
    tokenizer: str = Field(min_length=1)
    exclude_content_classes: list[str] = Field(
        default_factory=lambda: ["performance", "boilerplate", "navigation"]
    )

    @model_validator(mode="after")
    def _min_le_max(self) -> "ChunkingConfig":
        if self.min_tokens > self.max_tokens:
            raise ValueError("chunking.min_tokens must be <= chunking.max_tokens")
        return self


class EmbeddingConfig(_StrictModel):
    model: str = Field(min_length=1)
    device: str = "auto"
    batch_size: int = Field(default=32, gt=0)
    normalize: bool = True
    max_seq_length: int = Field(default=256, gt=0)


class VectorDBConfig(_StrictModel):
    provider: Literal["chromadb"] = "chromadb"
    persist_dir: str = Field(min_length=1)
    collection: str = Field(min_length=1)
    distance: Literal["cosine", "l2", "ip"] = "cosine"
    batch_size: int = Field(default=100, gt=0)


class RetrievalConfig(_StrictModel):
    top_k: int = Field(default=12, gt=0)
    final_k: int = Field(default=5, gt=0)
    score_threshold: float = Field(default=0.30, ge=0.0, le=1.0)
    max_context_tokens: int = Field(default=1200, gt=0)

    @model_validator(mode="after")
    def _final_le_top(self) -> "RetrievalConfig":
        if self.final_k > self.top_k:
            raise ValueError("retrieval.final_k must be <= retrieval.top_k")
        return self


class LLMConfig(_StrictModel):
    provider: str = ""
    model: str = ""
    temperature: float = Field(default=0.1, ge=0.0, le=2.0)
    max_tokens: int = Field(default=300, gt=0)
    timeout_seconds: float = Field(default=25.0, gt=0)
    max_retries: int = Field(default=2, ge=0)

    @field_validator("provider")
    @classmethod
    def _normalize_provider(cls, value: str) -> str:
        value = value.strip().lower()
        if value and value not in SUPPORTED_PROVIDERS:
            raise ValueError(
                f"unsupported LLM provider {value!r}; choose one of {SUPPORTED_PROVIDERS}"
            )
        return value


class GuardrailsConfig(_StrictModel):
    pii_block: bool = True
    refusals_enabled: bool = True
    citation_allowlist_only: bool = True
    max_sentences: int = Field(default=3, ge=1, le=3)
    repair_attempts: int = Field(default=1, ge=0, le=1)
    extractive_fallback: bool = True
    injection_defense: bool = True


class Settings(_StrictModel):
    app: AppConfig
    corpus: CorpusConfig
    chunking: ChunkingConfig
    embedding: EmbeddingConfig
    vector_db: VectorDBConfig
    retrieval: RetrievalConfig
    llm: LLMConfig
    guardrails: GuardrailsConfig

    config_path: Path = Field(default=DEFAULT_CONFIG_PATH, exclude=True)
    base_dir: Path = Field(default=PROJECT_ROOT, exclude=True)
    config_hash: str = Field(default="", exclude=True)

    @model_validator(mode="after")
    def _cross_validate(self) -> "Settings":
        if self.guardrails.max_sentences != self.app.max_answer_sentences:
            raise ValueError(
                "guardrails.max_sentences must equal app.max_answer_sentences "
                f"({self.guardrails.max_sentences} != {self.app.max_answer_sentences})"
            )
        if self.embedding.max_seq_length < self.chunking.max_tokens:
            raise ValueError(
                "embedding.max_seq_length must be >= chunking.max_tokens "
                f"({self.embedding.max_seq_length} < {self.chunking.max_tokens})"
            )
        if not self.config_hash:
            payload = self.model_dump(
                mode="json",
                exclude={"config_path", "base_dir", "config_hash"},
            )
            self.config_hash = _stable_hash(payload)
        return self

    @classmethod
    def load(
        cls,
        path: str | Path | None = None,
        *,
        base_dir: str | Path | None = None,
        require_llm: bool = False,
    ) -> "Settings":
        """Load and validate settings.

        ``require_llm=False`` lets a fresh checkout and the health check run
        without a provider key. Serving code should pass ``require_llm=True``.
        """
        root = Path(base_dir).resolve() if base_dir is not None else PROJECT_ROOT
        config_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
        if not config_path.is_absolute():
            config_path = (root / config_path).resolve()
        if not config_path.is_file():
            raise FileNotFoundError(f"config file not found: {config_path}")

        _load_dotenv(root / ".env")

        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"config root must be a mapping: {config_path}")

        data = _expand_env(raw)
        data["config_path"] = config_path
        data["base_dir"] = root
        settings = cls.model_validate(data)

        if require_llm:
            settings.require_llm()
        return settings

    @property
    def provider_key_env(self) -> str:
        return PROVIDER_ENV_KEYS.get(self.llm.provider, "")

    def provider_key_present(self) -> bool:
        key = self.provider_key_env
        return bool(key and os.environ.get(key))

    def require_llm(self) -> None:
        """Raise when the active provider is not ready to serve."""
        if not self.llm.provider:
            raise ValueError("LLM_PROVIDER is not set (copy .env.example to .env)")
        if not self.llm.model:
            raise ValueError("LLM_MODEL is not set (copy .env.example to .env)")
        if not self.provider_key_env:
            raise ValueError(f"no API key environment variable for provider {self.llm.provider!r}")
        if not self.provider_key_present():
            raise ValueError(f"{self.provider_key_env} is not set for provider {self.llm.provider!r}")

    def resolved_path(self, relative: str | Path) -> Path:
        """Resolve a config-relative path against the project root."""
        candidate = Path(relative)
        return candidate if candidate.is_absolute() else self.base_dir / candidate
