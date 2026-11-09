"""Provider adapters for Groq, Gemini, OpenAI, Anthropic, and Mistral.

Only the configured provider is called. Errors are sanitised: no API key,
authorization header, or raw provider payload is ever included in an exception
or a trace.

See ``implementation.md`` §7.6 (G43).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

from mf_faq.config import Settings

__all__ = [
    "LLMClient",
    "LLMCompletion",
    "LLMError",
    "ProviderClient",
    "build_llm_client",
]

OPENAI_COMPATIBLE_URLS: dict[str, str] = {
    "openai": "https://api.openai.com/v1/chat/completions",
    "groq": "https://api.groq.com/openai/v1/chat/completions",
    "mistral": "https://api.mistral.ai/v1/chat/completions",
}
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


class LLMError(RuntimeError):
    """The provider could not return a usable completion."""


@dataclass(frozen=True)
class LLMCompletion:
    text: str
    provider: str = ""
    model: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    latency_ms: float = 0.0


class LLMClient(Protocol):
    provider: str
    model: str

    def complete(self, *, system: str, user: str) -> LLMCompletion: ...


class ProviderClient:
    """HTTP adapter for the supported providers using OpenAI-style messages."""

    def __init__(
        self,
        settings: Settings,
        *,
        http_client: Any | None = None,
        sleep=time.sleep,
        clock=time.perf_counter,
    ) -> None:
        self.settings = settings
        self.provider = settings.llm.provider
        self.model = settings.llm.model
        self._http = http_client
        self._sleep = sleep
        self._clock = clock

    def _client(self) -> Any:
        if self._http is None:
            try:
                import httpx
            except ImportError as exc:  # pragma: no cover - declared dependency
                raise LLMError("httpx is not installed") from exc
            self._http = httpx.Client(timeout=self.settings.llm.timeout_seconds)
        return self._http

    def _key(self) -> str:
        import os

        env_name = self.settings.provider_key_env
        key = os.environ.get(env_name, "") if env_name else ""
        if not env_name:
            raise LLMError(f"no API key environment variable for provider {self.provider!r}")
        if not key:
            raise LLMError(f"{env_name} is not set for provider {self.provider!r}")
        return key

    def _request_spec(self, *, system: str, user: str) -> tuple[str, dict[str, str], dict[str, Any]]:
        key = self._key()
        temperature = self.settings.llm.temperature
        max_tokens = self.settings.llm.max_tokens

        if self.provider in OPENAI_COMPATIBLE_URLS:
            url = OPENAI_COMPATIBLE_URLS[self.provider]
            headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
            body = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
            return url, headers, body

        if self.provider == "anthropic":
            headers = {
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            }
            body = {
                "model": self.model,
                "system": system,
                "messages": [{"role": "user", "content": user}],
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
            return ANTHROPIC_URL, headers, body

        if self.provider == "gemini":
            url = GEMINI_URL.format(model=self.model)
            headers = {"Content-Type": "application/json"}
            body = {
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": {
                    "temperature": temperature,
                    "maxOutputTokens": max_tokens,
                },
            }
            return f"{url}?key={key}", headers, body

        raise LLMError(f"unsupported LLM provider {self.provider!r}")

    def _extract(self, payload: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        if self.provider in OPENAI_COMPATIBLE_URLS:
            try:
                text = payload["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError) as exc:
                raise LLMError("provider response did not contain a message") from exc
            usage = payload.get("usage") or {}
            return str(text), dict(usage)

        if self.provider == "anthropic":
            blocks = payload.get("content") or []
            text = "".join(
                str(block.get("text", ""))
                for block in blocks
                if isinstance(block, Mapping) and block.get("type", "text") == "text"
            )
            usage = payload.get("usage") or {}
            return text, dict(usage)

        if self.provider == "gemini":
            try:
                parts = payload["candidates"][0]["content"]["parts"]
                text = "".join(str(part.get("text", "")) for part in parts)
            except (KeyError, IndexError, TypeError) as exc:
                raise LLMError("provider response did not contain a candidate") from exc
            usage = payload.get("usageMetadata") or {}
            return text, dict(usage)

        raise LLMError(f"unsupported LLM provider {self.provider!r}")

    def complete(self, *, system: str, user: str) -> LLMCompletion:
        """Call the provider, retrying timeouts, 429s, and 5xx responses."""
        url, headers, body = self._request_spec(system=system, user=user)
        attempts = self.settings.llm.max_retries + 1
        last_error = ""

        for attempt in range(attempts):
            started = self._clock()
            try:
                response = self._client().post(url, headers=headers, json=body)
            except Exception as exc:  # httpx timeout / transport
                last_error = type(exc).__name__
                if attempt + 1 < attempts:
                    self._sleep(0.5 * (2**attempt))
                    continue
                raise LLMError(
                    f"{self.provider} request failed after {attempts} attempt(s) "
                    f"({last_error}); no payload logged"
                ) from None

            status_code = int(getattr(response, "status_code", 0))
            if status_code == 429 or status_code >= 500:
                last_error = f"HTTP {status_code}"
                if attempt + 1 < attempts:
                    self._sleep(0.5 * (2**attempt))
                    continue
                raise LLMError(
                    f"{self.provider} request failed after {attempts} attempt(s) "
                    f"({last_error}); no payload logged"
                )

            if status_code >= 400:
                raise LLMError(
                    f"{self.provider} request failed with HTTP {status_code}; "
                    "no payload logged"
                )

            try:
                payload = response.json()
            except Exception as exc:
                raise LLMError(f"{self.provider} returned invalid JSON") from exc
            if not isinstance(payload, Mapping):
                raise LLMError(f"{self.provider} returned an unexpected response shape")

            text, usage = self._extract(payload)
            if not text.strip():
                raise LLMError(f"{self.provider} returned an empty completion")
            latency_ms = (self._clock() - started) * 1000.0
            return LLMCompletion(
                text=text,
                provider=self.provider,
                model=self.model,
                usage=usage,
                latency_ms=latency_ms,
            )

        raise LLMError(f"{self.provider} request failed ({last_error})")


def build_llm_client(settings: Settings, **kwargs: Any) -> ProviderClient:
    """Build the configured provider client."""
    return ProviderClient(settings, **kwargs)
