"""Loader policy tests: allowlist, redirects, retries, size, robots."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx
import yaml

from mf_faq.config import PROVIDER_ENV_KEYS, SeedPage, Settings
from mf_faq.ingest.loader import (
    LoadError,
    OffAllowlistError,
    OversizedPageError,
    PageLoader,
    RobotsDisallowedError,
)

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "config" / "config.yaml"
PAGE_HTML = "<html><body><h1>Scheme</h1><p>" + ("fact " * 40) + "</p></body></html>"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    for key in PROVIDER_ENV_KEYS.values():
        monkeypatch.delenv(key, raising=False)


def make_settings(
    tmp_path: Path,
    *,
    delay: float = 0.0,
    retries: int = 0,
    max_page_bytes: int = 5 * 1024 * 1024,
) -> Settings:
    raw = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    raw["corpus"]["request_delay_seconds"] = delay
    raw["corpus"]["max_retries"] = retries
    raw["corpus"]["max_page_bytes"] = max_page_bytes
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return Settings.load(path, base_dir=tmp_path)


def seed(url: str = "https://groww.in/mutual-funds/hdfc-large-cap-fund-direct-growth") -> SeedPage:
    return SeedPage(id="c2_hdfc_large_cap", url=url, page_type="scheme", scheme_id="hdfc_large_cap")


@respx.mock
def test_exactly_five_fetches_for_five_seeds(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, delay=0.01)
    route = respx.route(method="GET", host="groww.in").mock(
        return_value=httpx.Response(200, text=PAGE_HTML)
    )
    sleeps: list[float] = []

    loader = PageLoader(settings, respect_robots=False, sleep=sleeps.append)
    try:
        batch = loader.fetch_all()
    finally:
        loader.close()

    assert not batch.failures
    assert len(batch.pages) == 5
    assert route.call_count == 5
    assert len(sleeps) == 4  # delay between pages, never after the last one
    raw_files = list((tmp_path / "data" / "raw").glob("*/*.html"))
    assert len(raw_files) == 5


def test_off_allowlist_seed_is_rejected_before_any_request(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    loader = PageLoader(settings, respect_robots=False, sleep=lambda _: None)
    try:
        with pytest.raises(OffAllowlistError, match="allowlist"):
            loader.fetch_one(seed("https://example.com/hdfc"))
    finally:
        loader.close()


@respx.mock
def test_off_domain_redirect_is_rejected(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    respx.get("https://groww.in/mutual-funds/hdfc-large-cap-fund-direct-growth").mock(
        return_value=httpx.Response(
            302,
            headers={"Location": "https://evil.example/elsewhere"},
        )
    )
    evil = respx.get("https://evil.example/elsewhere").mock(
        return_value=httpx.Response(200, text=PAGE_HTML)
    )

    loader = PageLoader(settings, respect_robots=False, sleep=lambda _: None)
    try:
        with pytest.raises(OffAllowlistError, match="allowlist"):
            loader.fetch_one(seed())
    finally:
        loader.close()

    assert evil.call_count == 1  # redirect was followed, then rejected


@respx.mock
def test_retry_then_success(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, retries=2)
    route = respx.get("https://groww.in/mutual-funds/hdfc-large-cap-fund-direct-growth").mock(
        side_effect=[
            httpx.Response(500),
            httpx.Response(503),
            httpx.Response(200, text=PAGE_HTML),
        ]
    )
    sleeps: list[float] = []

    loader = PageLoader(settings, respect_robots=False, sleep=sleeps.append)
    try:
        page = loader.fetch_one(seed())
    finally:
        loader.close()

    assert route.call_count == 3
    assert len(sleeps) == 2
    assert page.status_code == 200
    assert "fact" in page.html


@respx.mock
def test_retry_exhaustion_raises(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, retries=2)
    route = respx.get("https://groww.in/mutual-funds/hdfc-large-cap-fund-direct-growth").mock(
        return_value=httpx.Response(503)
    )

    loader = PageLoader(settings, respect_robots=False, sleep=lambda _: None)
    try:
        with pytest.raises(LoadError, match="3 attempts"):
            loader.fetch_one(seed())
    finally:
        loader.close()

    assert route.call_count == 3


@respx.mock
def test_oversized_page_is_rejected(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, max_page_bytes=200)
    respx.get("https://groww.in/mutual-funds/hdfc-large-cap-fund-direct-growth").mock(
        return_value=httpx.Response(200, text="<html>" + ("x" * 500) + "</html>")
    )

    loader = PageLoader(settings, respect_robots=False, sleep=lambda _: None)
    try:
        with pytest.raises(OversizedPageError, match="limit"):
            loader.fetch_one(seed())
    finally:
        loader.close()


@respx.mock
def test_robots_disallow_blocks_the_page_request(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    respx.get("https://groww.in/robots.txt").mock(
        return_value=httpx.Response(
            200, text="User-agent: *\nDisallow: /mutual-funds/\n"
        )
    )
    page_route = respx.get(
        "https://groww.in/mutual-funds/hdfc-large-cap-fund-direct-growth"
    ).mock(return_value=httpx.Response(200, text=PAGE_HTML))

    loader = PageLoader(settings, sleep=lambda _: None)
    try:
        with pytest.raises(RobotsDisallowedError):
            loader.fetch_one(seed())
    finally:
        loader.close()

    assert page_route.call_count == 0


@respx.mock
def test_missing_robots_txt_warns_and_continues(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    respx.get("https://groww.in/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://groww.in/mutual-funds/hdfc-large-cap-fund-direct-growth").mock(
        return_value=httpx.Response(200, text=PAGE_HTML)
    )

    loader = PageLoader(settings, sleep=lambda _: None)
    try:
        page = loader.fetch_one(seed())
    finally:
        loader.close()

    assert page.status_code == 200
    assert any("robots.txt unavailable" in warning for warning in page.warnings)
