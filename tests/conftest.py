"""Shared pytest configuration.

Tests must be hermetic: a developer's real ``.env`` (provider, model, API key)
must never leak into assertions or into settings used by fake services. The
autouse fixture disables ``.env`` loading for every test; production code still
loads it normally.
"""

from __future__ import annotations

import pytest

import mf_faq.config as config


@pytest.fixture(autouse=True)
def _isolate_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "_load_dotenv", lambda path: None)
    for key in (
        "LLM_PROVIDER",
        "LLM_MODEL",
        "EMBEDDING_MODEL_PATH",
        *config.PROVIDER_ENV_KEYS.values(),
    ):
        monkeypatch.delenv(key, raising=False)
