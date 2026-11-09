"""Load the source registry and resolve exactly one validated citation.

Model-suggested URLs are ignored. A factual citation always comes from the
selected evidence record; refusal citations come from the reference registry.
Every URL must be an exact member of the registry.

See ``implementation.md`` §7.5 and §9.5.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlsplit, urlunsplit

import yaml

from mf_faq.config import Settings
from mf_faq.contracts import Citation, RetrievedEvidence

__all__ = [
    "ReferenceLink",
    "ReferenceRegistry",
    "citation_from_evidence",
    "closest_corpus_citation",
    "is_allowlisted_citation",
    "load_registry",
    "normalize_url",
    "reference_by_id",
    "resolve_citation",
]

ADVICE_REFERENCE_IDS: tuple[str, ...] = (
    "ref_sebi_mutual_funds",
    "ref_sebi_investor",
    "ref_amfi_knowledge",
    "ref_amfi_faq",
)
PERFORMANCE_REFERENCE_IDS: tuple[str, ...] = (
    "ref_hdfc_factsheet",
    "ref_hdfc_downloads",
    "ref_hdfc_fund_page",
)


@dataclass(frozen=True)
class ReferenceLink:
    id: str
    title: str
    url: str
    kind: str = "reference"
    scheme_id: str | None = None
    page_type: str | None = None


@dataclass(frozen=True)
class ReferenceRegistry:
    corpus: tuple[ReferenceLink, ...]
    references: tuple[ReferenceLink, ...]

    @property
    def all(self) -> tuple[ReferenceLink, ...]:
        return (*self.corpus, *self.references)

    @property
    def corpus_urls(self) -> frozenset[str]:
        return frozenset(normalize_url(link.url) for link in self.corpus)

    @property
    def allowed_urls(self) -> frozenset[str]:
        return frozenset(normalize_url(link.url) for link in self.all)

    def corpus_for_scheme(self, scheme_id: str | None) -> ReferenceLink | None:
        if not scheme_id:
            return None
        for link in self.corpus:
            if link.scheme_id == scheme_id:
                return link
        return None


def normalize_url(url: str) -> str:
    """Normalize a URL for exact registry comparison."""
    parts = urlsplit((url or "").strip())
    scheme = parts.scheme.lower()
    host = parts.hostname.lower() if parts.hostname else ""
    port = f":{parts.port}" if parts.port else ""
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((scheme, f"{host}{port}", path, parts.query, ""))


def _link(payload: dict[str, Any], *, kind: str) -> ReferenceLink:
    return ReferenceLink(
        id=str(payload["id"]),
        title=str(payload.get("title") or payload.get("scheme_id") or payload["id"]),
        url=str(payload["url"]),
        kind=kind,
        scheme_id=payload.get("scheme_id"),
        page_type=payload.get("page_type"),
    )


def _build_registry(path: Path, settings: Settings | None) -> ReferenceRegistry:
    if path.is_file():
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        corpus = tuple(_link(item, kind="corpus") for item in raw.get("corpus", []))
        references = tuple(
            _link(item, kind="reference") for item in raw.get("references", [])
        )
        if corpus:
            return ReferenceRegistry(corpus=corpus, references=references)

    # Fallback: the configured five seed pages, with scheme IDs as titles.
    seed_links: list[ReferenceLink] = []
    if settings is not None:
        for seed in settings.corpus.seed_pages:
            seed_links.append(
                ReferenceLink(
                    id=seed.id,
                    title=seed.scheme_id,
                    url=seed.url,
                    kind="corpus",
                    scheme_id=seed.scheme_id,
                    page_type=seed.page_type,
                )
            )
    return ReferenceRegistry(corpus=tuple(seed_links), references=())


@lru_cache(maxsize=8)
def _cached_registry(path_str: str, mtime_ns: int) -> ReferenceRegistry:
    del mtime_ns  # part of the cache key only
    return _build_registry(Path(path_str), None)


def load_registry(settings: Settings) -> ReferenceRegistry:
    """Load ``docs/sources.yaml`` (cached by path and modification time)."""
    path = settings.resolved_path("docs/sources.yaml")
    if path.is_file():
        stat = path.stat()
        return _cached_registry(str(path), stat.st_mtime_ns)
    return _build_registry(path, settings)


def reference_by_id(registry: ReferenceRegistry, *ids: str) -> ReferenceLink | None:
    for reference_id in ids:
        for link in registry.references:
            if link.id == reference_id:
                return link
    return None


def is_allowlisted_citation(url: str, registry: ReferenceRegistry) -> bool:
    return normalize_url(url) in registry.allowed_urls


def _citation(link: ReferenceLink, *, section: str | None = None) -> Citation:
    return Citation(url=link.url, title=link.title, section=section)


def citation_from_evidence(
    evidence: Sequence[RetrievedEvidence],
    registry: ReferenceRegistry,
) -> Citation | None:
    """Return the citation of the best evidence record, if it is a corpus URL."""
    for item in evidence:
        if normalize_url(item.url) in registry.corpus_urls:
            return Citation(
                url=item.url,
                title=item.title,
                section=item.section or None,
            )
    return None


def closest_corpus_citation(
    *,
    resolved_scheme: str | None,
    evidence: Sequence[RetrievedEvidence],
    registry: ReferenceRegistry,
) -> Citation | None:
    """Pick one corpus page: evidence first, then the resolved scheme page."""
    from_evidence = citation_from_evidence(evidence, registry)
    if from_evidence is not None:
        return from_evidence
    link = registry.corpus_for_scheme(resolved_scheme)
    if link is None and registry.corpus:
        # The AMC overview is the safest closest page for an unresolved query.
        link = next(
            (item for item in registry.corpus if item.page_type == "amc"),
            registry.corpus[0],
        )
    return _citation(link) if link else None


def resolve_citation(
    *,
    status: str,
    registry: ReferenceRegistry,
    evidence: Sequence[RetrievedEvidence] = (),
    resolved_scheme: str | None = None,
    resolved_field: str | None = None,
    model_url: str | None = None,
) -> Citation | None:
    """Resolve exactly one citation for a status.

    ``model_url`` is accepted only so callers can pass it through; it never
    influences the result. This is the guard that keeps invented URLs out.
    """
    del model_url

    if status == "answered":
        return citation_from_evidence(evidence, registry)

    if status in {"not_found", "error"}:
        citation = closest_corpus_citation(
            resolved_scheme=resolved_scheme,
            evidence=evidence,
            registry=registry,
        )
        if citation is None and status == "not_found" and resolved_field == "statement_download":
            link = reference_by_id(registry, "ref_hdfc_downloads")
            if link:
                return _citation(link)
        return citation

    if status == "refused_advice":
        link = reference_by_id(registry, *ADVICE_REFERENCE_IDS)
        return _citation(link) if link else None

    if status == "refused_performance":
        link = reference_by_id(registry, *PERFORMANCE_REFERENCE_IDS)
        return _citation(link) if link else None

    return None
