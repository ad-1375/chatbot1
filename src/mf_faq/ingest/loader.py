"""Fetch and snapshot exactly the five configured seed pages.

Policy (implementation.md §3.2):

* only URLs already on the corpus allowlist are ever requested;
* redirects are followed, but the final host must stay on the allowlist;
* one ``robots.txt`` fetch per domain per run (fail-open with a warning);
* bounded retries with exponential backoff, request delay between pages;
* oversized responses are rejected before extraction.

The loader performs network I/O only. Extraction and artifact writing live in
``mf_faq.ingest.extract`` and ``scripts.run_ingest`` respectively.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable
from urllib.robotparser import RobotFileParser
from urllib.parse import urlparse

import httpx

from mf_faq.config import SeedPage, Settings
from mf_faq.io_utils import write_text

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class LoadError(RuntimeError):
    """Base class for ingestion failures."""


class OffAllowlistError(LoadError):
    """A seed or redirect target is not on the corpus allowlist."""


class OversizedPageError(LoadError):
    """A response body exceeds ``corpus.max_page_bytes``."""


class RobotsDisallowedError(LoadError):
    """``robots.txt`` disallows the URL for our user agent."""


class FetchStatusError(LoadError):
    """The server returned a non-success status after all retries."""


@dataclass
class FetchedPage:
    """A successfully fetched page plus its provenance."""

    seed: SeedPage
    final_url: str
    status_code: int
    html: str
    fetched_at: datetime
    renderer: str = "httpx"
    raw_path: Path | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass
class LoadFailure:
    seed: SeedPage
    error: str
    error_type: str


@dataclass
class LoadBatch:
    pages: list[FetchedPage] = field(default_factory=list)
    failures: list[LoadFailure] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.failures and len(self.pages) == 5


def host_allowed(host: str, allowlist: Iterable[str]) -> bool:
    """Return True when ``host`` equals or is a subdomain of an allowed host."""
    normalized = (host or "").lower().strip(".")
    return any(
        normalized == domain or normalized.endswith(f".{domain}")
        for domain in (item.lower().strip(".") for item in allowlist)
    )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class _RobotsCache:
    """Fetch and cache ``robots.txt`` once per domain per loader run."""

    def __init__(
        self,
        client: httpx.Client,
        *,
        user_agent: str,
        timeout: float,
    ) -> None:
        self._client = client
        self._user_agent = user_agent
        self._timeout = timeout
        self._cache: dict[str, RobotFileParser | None] = {}

    def allowed(self, url: str) -> tuple[bool, list[str]]:
        parsed = urlparse(url)
        domain = (parsed.hostname or "").lower()
        if not domain:
            return False, [f"cannot determine host for {url!r}"]

        warnings: list[str] = []
        if domain not in self._cache:
            robots_url = f"{parsed.scheme or 'https'}://{domain}/robots.txt"
            try:
                response = self._client.get(
                    robots_url,
                    timeout=self._timeout,
                    headers={"User-Agent": self._user_agent},
                )
                if response.status_code == 200:
                    parser = RobotFileParser()
                    parser.set_url(robots_url)
                    parser.parse(response.text.splitlines())
                    self._cache[domain] = parser
                else:
                    warnings.append(
                        f"robots.txt unavailable ({response.status_code}); continuing"
                    )
                    self._cache[domain] = None
            except httpx.HTTPError as exc:
                warnings.append(f"robots.txt fetch failed ({type(exc).__name__}); continuing")
                self._cache[domain] = None

        parser = self._cache[domain]
        if parser is None:
            return True, warnings
        return parser.can_fetch(self._user_agent, url), warnings


class PageLoader:
    """Fetch the five seed pages without crawling a single link."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = _utcnow,
        respect_robots: bool = True,
        retry_base_seconds: float = 2.0,
    ) -> None:
        self.settings = settings
        self._sleep = sleep
        self._clock = clock
        self._owns_client = client is None
        self._client = client or httpx.Client(
            follow_redirects=True,
            timeout=settings.corpus.timeout_seconds,
            headers={"User-Agent": settings.corpus.user_agent},
        )
        self._respect_robots = respect_robots
        self._retry_base_seconds = retry_base_seconds
        self._robots = _RobotsCache(
            self._client,
            user_agent=settings.corpus.user_agent,
            timeout=settings.corpus.timeout_seconds,
        )

    # -- lifecycle -----------------------------------------------------
    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "PageLoader":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- public API ----------------------------------------------------
    def fetch_all(self) -> LoadBatch:
        """Fetch every seed, keeping going so one failure cannot hide others."""
        batch = LoadBatch()
        delay = self.settings.corpus.request_delay_seconds
        for index, seed in enumerate(self.settings.corpus.seed_pages):
            if index and delay > 0:
                self._sleep(delay)
            try:
                batch.pages.append(self.fetch_one(seed))
            except LoadError as exc:
                batch.failures.append(
                    LoadFailure(seed=seed, error=str(exc), error_type=type(exc).__name__)
                )
        return batch

    def fetch_one(self, seed: SeedPage) -> FetchedPage:
        self._assert_allowed(seed.url)

        warnings: list[str] = []
        if self._respect_robots:
            allowed, robot_warnings = self._robots.allowed(seed.url)
            warnings.extend(robot_warnings)
            if not allowed:
                raise RobotsDisallowedError(
                    f"robots.txt disallows {seed.url} for {self.settings.corpus.user_agent!r}"
                )

        response = self._request_with_retries(seed.url)
        final_url = str(response.url)
        self._assert_allowed(final_url)

        content = response.content
        if len(content) > self.settings.corpus.max_page_bytes:
            raise OversizedPageError(
                f"{seed.id}: response is {len(content)} bytes "
                f"(limit {self.settings.corpus.max_page_bytes})"
            )

        fetched_at = self._clock()
        html = content.decode(response.encoding or "utf-8", errors="replace")
        raw_path = self._snapshot(seed, html, fetched_at)

        return FetchedPage(
            seed=seed,
            final_url=final_url,
            status_code=response.status_code,
            html=html,
            fetched_at=fetched_at,
            renderer="httpx",
            raw_path=raw_path,
            warnings=warnings,
        )

    # -- internals -----------------------------------------------------
    def _request_with_retries(self, url: str) -> httpx.Response:
        retries = self.settings.corpus.max_retries
        last_error: Exception | None = None

        for attempt in range(retries + 1):
            try:
                response = self._client.get(
                    url,
                    timeout=self.settings.corpus.timeout_seconds,
                    headers={"User-Agent": self.settings.corpus.user_agent},
                )
            except httpx.HTTPError as exc:
                last_error = exc
            else:
                if response.status_code not in RETRYABLE_STATUS:
                    if response.status_code >= 400:
                        raise FetchStatusError(
                            f"{url} returned HTTP {response.status_code}"
                        )
                    return response
                last_error = FetchStatusError(
                    f"{url} returned HTTP {response.status_code}"
                )

            if attempt < retries:
                self._sleep(self._backoff(attempt))

        raise LoadError(f"fetch failed after {retries + 1} attempts: {last_error}") from last_error

    def _backoff(self, attempt: int) -> float:
        return self._retry_base_seconds * (2**attempt)

    def _assert_allowed(self, url: str) -> None:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme not in {"http", "https"} or not host:
            raise OffAllowlistError(f"not an absolute http(s) URL: {url!r}")
        if not host_allowed(host, self.settings.corpus.allowlist_domains):
            raise OffAllowlistError(
                f"host {host!r} is not on the allowlist "
                f"{self.settings.corpus.allowlist_domains}"
            )

    def _snapshot(self, seed: SeedPage, html: str, fetched_at: datetime) -> Path:
        stamp = fetched_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        path = self.settings.resolved_path(f"data/raw/{seed.id}/{stamp}.html")
        write_text(path, html)
        return path
