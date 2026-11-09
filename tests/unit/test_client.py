"""Provider retry and safe-error tests (G43)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mf_faq.config import Settings
from mf_faq.generation.client import LLMError, ProviderClient

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "config" / "config.yaml"
SECRET = "test-secret-key-do-not-log"


class FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload


class FakeHTTP:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    def post(self, url: str, *, headers: dict, json: dict) -> FakeResponse:
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome  # type: ignore[return-value]


def completion_payload(text: str = "ok") -> dict:
    return {
        "choices": [{"message": {"content": text}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }


@pytest.fixture()
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("GROQ_API_KEY", SECRET)
    base = Settings.load(CONFIG)
    return base.model_copy(
        update={
            "llm": base.llm.model_copy(
                update={
                    "provider": "groq",
                    "model": "test-model",
                    "max_retries": 2,
                    "timeout_seconds": 5.0,
                }
            )
        }
    )


def build(settings: Settings, http: FakeHTTP, sleeps: list[float]) -> ProviderClient:
    return ProviderClient(settings, http_client=http, sleep=sleeps.append)


def test_timeout_is_retried_then_succeeds(settings: Settings) -> None:
    sleeps: list[float] = []
    http = FakeHTTP([TimeoutError("boom"), FakeResponse(200, completion_payload("hello"))])
    client = build(settings, http, sleeps)

    result = client.complete(system="s", user="u")

    assert result.text == "hello"
    assert http.calls == 2
    assert sleeps == [0.5]


def test_rate_limit_and_5xx_are_retried_with_backoff(settings: Settings) -> None:
    sleeps: list[float] = []
    http = FakeHTTP(
        [
            FakeResponse(429),
            FakeResponse(503),
            FakeResponse(200, completion_payload("recovered")),
        ]
    )
    client = build(settings, http, sleeps)

    result = client.complete(system="s", user="u")

    assert result.text == "recovered"
    assert http.calls == 3
    assert sleeps == [0.5, 1.0]


def test_persistent_timeout_raises_safe_error_without_secret(settings: Settings) -> None:
    sleeps: list[float] = []
    http = FakeHTTP([TimeoutError("boom"), TimeoutError("boom"), TimeoutError("boom")])
    client = build(settings, http, sleeps)

    with pytest.raises(LLMError) as excinfo:
        client.complete(system="s", user="u")

    message = str(excinfo.value)
    assert "after 3 attempt(s)" in message
    assert "no payload logged" in message
    assert SECRET not in message
    assert http.calls == 3


def test_persistent_500_raises_safe_error(settings: Settings) -> None:
    sleeps: list[float] = []
    http = FakeHTTP([FakeResponse(500), FakeResponse(502), FakeResponse(504)])
    client = build(settings, http, sleeps)

    with pytest.raises(LLMError) as excinfo:
        client.complete(system="s", user="u")

    assert "HTTP 504" in str(excinfo.value)
    assert SECRET not in str(excinfo.value)


def test_non_retryable_4xx_fails_fast(settings: Settings) -> None:
    sleeps: list[float] = []
    http = FakeHTTP([FakeResponse(400, {"error": "bad request"})])
    client = build(settings, http, sleeps)

    with pytest.raises(LLMError) as excinfo:
        client.complete(system="s", user="u")

    assert "HTTP 400" in str(excinfo.value)
    assert http.calls == 1
    assert sleeps == []


def test_missing_key_is_a_safe_error(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    http = FakeHTTP([])
    client = build(settings, http, [])

    with pytest.raises(LLMError) as excinfo:
        client.complete(system="s", user="u")

    assert "GROQ_API_KEY is not set" in str(excinfo.value)
    assert http.calls == 0


def test_empty_completion_is_rejected(settings: Settings) -> None:
    http = FakeHTTP([FakeResponse(200, completion_payload("   "))])
    client = build(settings, http, [])

    with pytest.raises(LLMError):
        client.complete(system="s", user="u")
