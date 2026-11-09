"""Citation resolution and allowlist tests (§7.5, §9.5)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from mf_faq.config import Settings
from mf_faq.contracts import RetrievedEvidence
from mf_faq.generation.citation import (
    citation_from_evidence,
    is_allowlisted_citation,
    load_registry,
    normalize_url,
    resolve_citation,
)

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "config" / "config.yaml"
ELSS_URL = "https://groww.in/mutual-funds/hdfc-elss-tax-saver-fund-direct-plan-growth"
INVENTED_URL = "https://evil.example/invented-citation"
NOW = datetime(2026, 11, 9, 10, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def settings() -> Settings:
    return Settings.load(CONFIG)


@pytest.fixture(scope="module")
def registry(settings: Settings):
    return load_registry(settings)


def evidence(*, url: str = ELSS_URL) -> RetrievedEvidence:
    return RetrievedEvidence(
        chunk_id="chunk-elss",
        doc_id="c4_hdfc_elss",
        url=url,
        title="HDFC ELSS Tax Saver Fund Direct Plan Growth",
        section="Fund details > Expense ratio",
        field="expense_ratio",
        score=0.91,
        text="HDFC ELSS Tax Saver Fund Direct Plan Growth > Expense ratio: 1.20%",
        fetched_at=NOW,
    )


def test_answered_uses_evidence_url_not_model_url(registry) -> None:
    citation = resolve_citation(
        status="answered",
        registry=registry,
        evidence=[evidence()],
        model_url=INVENTED_URL,
    )

    assert citation is not None
    assert normalize_url(citation.url) == normalize_url(ELSS_URL)
    assert citation.url != INVENTED_URL


def test_answered_rejects_off_allowlist_evidence(registry) -> None:
    citation = resolve_citation(
        status="answered",
        registry=registry,
        evidence=[evidence(url=INVENTED_URL)],
    )

    assert citation is None
    assert citation_from_evidence([evidence(url=INVENTED_URL)], registry) is None
    assert is_allowlisted_citation(INVENTED_URL, registry) is False


def test_allowlist_contains_corpus_and_references(registry) -> None:
    assert len(registry.corpus) == 5
    assert 10 <= len(registry.references) <= 15
    for link in registry.all:
        assert is_allowlisted_citation(link.url, registry) is True


def test_advice_refusal_uses_one_education_link(registry) -> None:
    citation = resolve_citation(status="refused_advice", registry=registry)

    assert citation is not None
    assert is_allowlisted_citation(citation.url, registry) is True
    assert citation.url in {
        "https://www.sebi.gov.in/financial-education/mutual-funds.html",
        "https://investor.sebi.gov.in/",
        "https://www.amfiindia.com/investor-corner/knowledge-center",
        "https://www.amfiindia.com/investor-corner/knowledge-center/mutual-funds-faqs.html",
    }


def test_performance_refusal_uses_factsheet_link(registry) -> None:
    citation = resolve_citation(status="refused_performance", registry=registry)

    assert citation is not None
    assert citation.url == "https://www.hdfcfund.com/mutual-funds"


def test_clarify_pii_and_out_of_scope_have_no_citation(registry) -> None:
    for status in ("clarify", "blocked_pii", "out_of_scope"):
        assert resolve_citation(status=status, registry=registry) is None


def test_not_found_uses_closest_scheme_page(registry) -> None:
    citation = resolve_citation(
        status="not_found",
        registry=registry,
        resolved_scheme="hdfc_large_cap",
    )

    assert citation is not None
    assert citation.url == "https://groww.in/mutual-funds/hdfc-large-cap-fund-direct-growth"
    assert is_allowlisted_citation(citation.url, registry) is True


def test_error_uses_closest_corpus_page(registry) -> None:
    citation = resolve_citation(status="error", registry=registry)

    assert citation is not None
    assert is_allowlisted_citation(citation.url, registry) is True
    assert citation.url in {link.url for link in registry.corpus}


def test_all_corpus_urls_are_groww_scheme_pages(registry) -> None:
    for link in registry.corpus:
        assert "groww.in" in link.url
        assert link.title
