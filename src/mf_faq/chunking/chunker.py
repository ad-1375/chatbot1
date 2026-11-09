"""Build atomic factual chunks with section context.

The chunker consumes the Step 1 block artifact and emits one chunk per atomic
unit:

* ``kv_pair`` and ``table_row`` blocks are never split;
* paragraphs above the token limit are split at sentence boundaries only;
* an atomic unit that is still too large is kept for audit with
  ``indexed: false`` and ``oversize: true``;
* performance, boilerplate, and navigation content is kept in the audit trail
  but excluded from the vector index.

See implementation.md §4.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from mf_faq.config import Settings
from mf_faq.contracts import Chunk, ContentClass, SourcePage
from mf_faq.chunking.taxonomy import CANONICAL_FIELDS, field_display, resolve_field

_FACT_FIELDS: frozenset[str] = frozenset(
    field for field in CANONICAL_FIELDS if field != "other"
)

_PERFORMANCE_SECTIONS: tuple[str, ...] = (
    "return calculator",
    "returns calculator",
    "returns and rankings",
    "holdings",
    "nav history",
    "performance",
)

_BOILERPLATE_SECTIONS: tuple[str, ...] = (
    "let s have a closer look",
    "list of hdfc mutual fund",
    "how to invest",
    "fund management",
    "why invest",
    "similar funds",
    "mutual fund categories",
    "fund categories",
    "top amcs",
)

_PERFORMANCE_TEXT: tuple[str, ...] = (
    "fund returns",
    "historic returns",
    "absolute returns",
    "annualised returns",
    "annualised",
    "category average",
    "historic performance",
    "latest nav",
    "nav as of",
    "over the past",
    "total investment",
    "would ve become",
    "would've become",
    "nav:",
    "cagr",
    "sharpe",
    "alpha",
    "beta",
)

_BOILERPLATE_TEXT: tuple[str, ...] = (
    "also manages these schemes",
    "scheme information document",
    "view details",
    "why invest",
    "invest in stocks",
    "open demat account",
    "download the groww app",
    "similar funds",
    "rating",
)

_NAVIGATION_TEXT: tuple[str, ...] = (
    "groww is a registered",
    "registered intermediary",
    "subject to market risks",
    "read all scheme related documents",
    "privacy policy",
    "terms and conditions",
    "cookie",
    "footer",
)

_CONTACT_LABELS: frozenset[str] = frozenset(
    {"phone", "e mail", "email", "website", "address"}
)

_ATOMIC_TYPES = frozenset({"kv_pair", "table_row"})
_ALLOWED_TYPES = frozenset({"kv_pair", "table_row", "list_item", "paragraph"})

_NUMERIC_PERCENT_RE = re.compile(
    r"^[+\-]?\d[\d,.]*\s*%?(?:\s*:\s*%?)?$"
)
_CONTACT_PLACEHOLDER_RE = re.compile(r"\[(?:email|phone) removed\]", re.IGNORECASE)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_WHITESPACE_RE = re.compile(r"\s+")
_HASH_RE = re.compile(r"[^a-z0-9%]+")

_ABBREVIATIONS: frozenset[str] = frozenset(
    {
        "mr",
        "mrs",
        "ms",
        "dr",
        "rs",
        "ltd",
        "inc",
        "co",
        "corp",
        "vs",
        "etc",
        "e.g",
        "i.e",
        "no",
        "st",
        "approx",
    }
)

_TOKEN_RE = re.compile(r"\w+|[^\w\s]", re.UNICODE)


class ChunkingError(RuntimeError):
    """The chunker could not build a consistent artifact set."""


class ChunkInvariantError(ChunkingError):
    """One or more §4.6 invariants failed."""


class TokenCounter:
    """Count tokens with the configured MiniLM tokenizer when available.

    The heavy embedding dependencies are optional for Step 2. When the
    tokenizer cannot be loaded, a deterministic word/punctuation counter is
    used and the report records ``tokenizer_fallback: true``.
    """

    def __init__(self, model_name: str, *, count_fn: Callable[[str], int] | None = None) -> None:
        self.model_name = model_name
        self.fallback = False
        self._count_fn = count_fn
        if self._count_fn is None:
            tokenizer = _load_tokenizer(model_name)
            if tokenizer is None:
                self.fallback = True
                self._count_fn = _heuristic_count
            else:
                self._count_fn = lambda text: len(
                    tokenizer.encode(text, add_special_tokens=False)
                )

    def count(self, text: str) -> int:
        return int(self._count_fn(text))


def _load_tokenizer(model_name: str) -> Any | None:
    try:  # optional dependency: Step 2 must run before embeddings are installed
        from transformers import AutoTokenizer
    except ImportError:
        return None
    try:
        return AutoTokenizer.from_pretrained(model_name, local_files_only=True)
    except Exception:  # pragma: no cover - depends on local model cache
        return None


def _heuristic_count(text: str) -> int:
    return len(_TOKEN_RE.findall(text))


def _norm(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", _HASH_RE.sub(" ", (text or "").lower())).strip()


def _get(block: Any, name: str, default: Any = None) -> Any:
    if isinstance(block, dict):
        return block.get(name, default)
    return getattr(block, name, default)


def _ends_with_abbreviation(text: str) -> bool:
    stripped = text.rstrip()
    if not stripped.endswith("."):
        return False
    word = stripped[:-1].split()[-1].lower() if stripped[:-1].split() else ""
    return word in _ABBREVIATIONS or (len(word) == 1 and word.isalpha())


def split_sentences(text: str) -> list[str]:
    """Split prose at sentence boundaries without breaking common abbreviations."""
    parts = [part.strip() for part in _SENTENCE_SPLIT_RE.split(text.strip()) if part.strip()]
    merged: list[str] = []
    for part in parts:
        if merged and _ends_with_abbreviation(merged[-1]):
            merged[-1] = f"{merged[-1]} {part}"
        else:
            merged.append(part)
    return merged or [text.strip()] if text.strip() else []


def _pack_sentences(
    sentences: list[str],
    *,
    max_tokens: int,
    count: Callable[[str], int],
) -> list[str]:
    if not sentences:
        return []
    pieces: list[str] = []
    current: list[str] = []
    for sentence in sentences:
        candidate = " ".join([*current, sentence])
        if current and count(candidate) > max_tokens:
            pieces.append(" ".join(current))
            current = [sentence]
        else:
            current.append(sentence)
    if current:
        pieces.append(" ".join(current))
    return pieces


@dataclass
class _Draft:
    section_path: list[str]
    field: str
    chunk_type: str
    content_class: ContentClass
    body: str
    exclusion_reason: str | None = None
    oversize: bool = False
    display: str | None = None

    def with_body(self, body: str) -> "_Draft":
        return _Draft(
            section_path=list(self.section_path),
            field=self.field,
            chunk_type=self.chunk_type,
            content_class=self.content_class,
            body=body,
            exclusion_reason=self.exclusion_reason,
            oversize=self.oversize,
            display=self.display,
        )


def _display_for(field: str, chunk_type: str) -> str:
    if field != "other":
        return field_display(field)
    return {
        "paragraph": "Description",
        "table_row": "Table row",
        "list_item": "List item",
    }.get(chunk_type, "Fact")


def _label_parts(label: str) -> tuple[str, str | None]:
    """Split ``Label: value`` labels emitted by two-cell layout wrappers."""
    cleaned = _WHITESPACE_RE.sub(" ", label).strip().rstrip(":").strip()
    if ":" in cleaned:
        head, tail = cleaned.split(":", 1)
        head, tail = head.strip(), tail.strip()
        if head and tail:
            return head, tail
    return cleaned, None


def render_chunk_text(title: str, draft: _Draft) -> str:
    """Render ``title > section > field: fact`` with no duplicated context."""
    section = list(draft.section_path)
    norm_title = _norm(title)
    while section and _norm(section[0]) == norm_title:
        section.pop(0)
    display = draft.display or _display_for(draft.field, draft.chunk_type)
    if section and _norm(section[-1]) == _norm(display):
        section = section[:-1]
    parts = [title, *section, display]
    prefix = " > ".join(part for part in parts if part)
    body = _WHITESPACE_RE.sub(" ", draft.body).strip()
    return f"{prefix}: {body}"


def _is_contact(label: str | None, text: str) -> bool:
    normalised = _norm(label or "")
    if normalised in _CONTACT_LABELS:
        return True
    if normalised.startswith(("phone ", "e mail ", "email ", "website ", "address ")):
        return True
    text_norm = _norm(text)
    if text_norm.startswith(("phone ", "e mail ", "email ", "website ", "address ")):
        return True
    return bool(_CONTACT_PLACEHOLDER_RE.search(text))


def _section_contains(section_text: str, markers: Iterable[str]) -> str | None:
    for marker in markers:
        if marker in section_text:
            return marker
    return None


def _classify(
    *,
    field: str,
    label: str | None,
    text: str,
    header: list[str] | None,
    section_path: list[str],
) -> tuple[ContentClass, str | None]:
    section_text = _norm(" ".join(section_path))
    text_norm = _norm(text)

    marker = _section_contains(section_text, _PERFORMANCE_SECTIONS)
    if marker:
        return "performance", f"performance: {marker}"

    marker = _section_contains(section_text, _BOILERPLATE_SECTIONS)
    if marker:
        return "boilerplate", f"boilerplate: {marker}"

    marker = _section_contains(text_norm, _NAVIGATION_TEXT)
    if marker:
        return "navigation", f"navigation: {marker}"

    if _is_contact(label, text):
        return "boilerplate", "boilerplate: contact detail"

    marker = _section_contains(text_norm, _BOILERPLATE_TEXT)
    if marker:
        return "boilerplate", f"boilerplate: {marker}"

    if field not in _FACT_FIELDS:
        marker = _section_contains(text_norm, _PERFORMANCE_TEXT)
        if marker:
            return "performance", f"performance: {marker}"
        label_norm = _norm(label or "")
        if label_norm == "nav" or text_norm.startswith("nav "):
            return "navigation", "navigation: nav value"
        if header:
            heads = {_norm(item) for item in header}
            if heads & {"3y", "5y", "10y", "1y", "all"}:
                return "performance", "performance: return-period table"
        if _NUMERIC_PERCENT_RE.match((label or "").strip()) or _NUMERIC_PERCENT_RE.match(
            text.strip()
        ):
            return "performance", "performance: return value"

    return "factual", None


def _body_for(block: Any, chunk_type: str) -> str:
    if chunk_type == "kv_pair":
        value = _get(block, "value")
        if value:
            return str(value)
    return str(_get(block, "text") or "")


def _block_to_drafts(
    block: Any,
    *,
    title: str,
    counter: TokenCounter,
    max_tokens: int,
) -> list[_Draft]:
    kind = str(_get(block, "kind") or "other")
    if kind == "heading":
        return []
    chunk_type = kind if kind in _ALLOWED_TYPES else "other"
    section_path = [str(item) for item in (_get(block, "section_path") or [])]
    label = _get(block, "label")
    header = _get(block, "header")
    text = str(_get(block, "text") or "")
    value = _get(block, "value")
    if not text and value:
        text = str(value)
    if not text.strip():
        return []

    field_section = list(section_path)
    if field_section and _norm(field_section[0]) == _norm(title):
        field_section = field_section[1:]

    if chunk_type == "paragraph" and not label:
        # Prose can mention many fields. Only short standalone paragraphs are
        # trusted to name their own field; long prose stays ``other`` so a
        # section such as "About ... Direct Plan Growth" cannot mislabel it.
        if len(text.split()) <= 12:
            field = resolve_field(text=text, section_path=field_section)
        else:
            field = "other"
    else:
        field = resolve_field(
            label=str(label) if label else None,
            text=text,
            header=header,
            section_path=field_section,
        )
    content_class, reason = _classify(
        field=field,
        label=str(label) if label else None,
        text=text,
        header=list(header) if header else None,
        section_path=section_path,
    )

    body = _body_for(block, chunk_type)
    if chunk_type == "kv_pair" and label and value:
        body = str(value)
    if not body.strip():
        body = text

    display: str | None = None
    if chunk_type == "kv_pair" and field == "other" and label:
        head, tail = _label_parts(str(label))
        if head:
            display = head
        if tail:
            body = tail

    draft = _Draft(
        section_path=section_path,
        field=field,
        chunk_type=chunk_type,
        content_class=content_class,
        body=body,
        exclusion_reason=reason,
        display=display,
    )

    if content_class != "factual" or chunk_type != "paragraph":
        return [draft]

    rendered = render_chunk_text(title, draft)
    if counter.count(rendered) <= max_tokens:
        return [draft]

    sentences = split_sentences(draft.body)
    # Measure the rendered chunk (prefix included), not just the sentence text.
    rendered_count = lambda piece: counter.count(
        render_chunk_text(title, draft.with_body(piece))
    )
    pieces = _pack_sentences(sentences, max_tokens=max_tokens, count=rendered_count)
    pieces = [piece for piece in pieces if piece.strip()]
    if len(pieces) <= 1:
        return [draft]
    return [draft.with_body(piece) for piece in pieces]


def _merge_prose(
    drafts: list[_Draft],
    *,
    title: str,
    counter: TokenCounter,
    max_tokens: int,
    min_tokens: int,
) -> list[_Draft]:
    """Merge adjacent small factual paragraphs in the same section and field."""
    merged: list[_Draft] = []
    for draft in drafts:
        if (
            merged
            and draft.chunk_type == "paragraph"
            and merged[-1].chunk_type == "paragraph"
            and draft.content_class == "factual"
            and merged[-1].content_class == "factual"
            and draft.section_path == merged[-1].section_path
            and draft.field == merged[-1].field
        ):
            candidate = merged[-1].with_body(f"{merged[-1].body} {draft.body}")
            current_tokens = counter.count(render_chunk_text(title, merged[-1]))
            candidate_tokens = counter.count(render_chunk_text(title, candidate))
            if (
                current_tokens < min_tokens
                and candidate_tokens <= max_tokens
            ):
                merged[-1] = candidate
                continue
        merged.append(draft)
    return merged


def _dedupe(drafts: list[_Draft], *, title: str) -> list[_Draft]:
    seen: set[str] = set()
    unique: list[_Draft] = []
    for draft in drafts:
        key = _norm(render_chunk_text(title, draft))
        if key in seen:
            continue
        seen.add(key)
        unique.append(draft)
    return unique


def _chunk_id(doc_id: str, draft: _Draft, ordinal: int) -> str:
    payload = "|".join(
        [doc_id, *draft.section_path, draft.field, str(ordinal)]
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def _content_hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def chunk_document(
    source: SourcePage,
    blocks: Iterable[Any],
    settings: Settings,
    *,
    token_counter: TokenCounter | None = None,
) -> list[Chunk]:
    """Chunk one page into atomic, context-prefixed facts."""
    counter = token_counter or TokenCounter(settings.chunking.tokenizer)
    max_tokens = settings.chunking.max_tokens
    min_tokens = settings.chunking.min_tokens
    exclude_classes = set(settings.chunking.exclude_content_classes)

    drafts: list[_Draft] = []
    for block in blocks:
        drafts.extend(
            _block_to_drafts(
                block,
                title=source.title,
                counter=counter,
                max_tokens=max_tokens,
            )
        )

    drafts = _merge_prose(
        drafts,
        title=source.title,
        counter=counter,
        max_tokens=max_tokens,
        min_tokens=min_tokens,
    )
    drafts = _dedupe(drafts, title=source.title)

    chunks: list[Chunk] = []
    ordinals: dict[tuple[tuple[str, ...], str], int] = {}
    for draft in drafts:
        text = render_chunk_text(source.title, draft)
        token_count = counter.count(text)
        oversize = draft.oversize or token_count > max_tokens
        if oversize and draft.chunk_type in _ATOMIC_TYPES:
            reason = "oversize: atomic unit exceeds max_tokens"
        elif oversize:
            reason = "oversize: sentence exceeds max_tokens"
        else:
            reason = draft.exclusion_reason

        # An oversize factual chunk is kept whole for audit but never indexed.
        if oversize and reason is None:
            reason = "oversize"
        indexed = draft.content_class not in exclude_classes and not oversize
        if not indexed and reason is None:
            reason = f"{draft.content_class} content"

        key = (tuple(draft.section_path), draft.field)
        ordinal = ordinals.get(key, 0)
        ordinals[key] = ordinal + 1

        chunks.append(
            Chunk(
                chunk_id=_chunk_id(source.doc_id, draft, ordinal),
                doc_id=source.doc_id,
                url=source.url,
                title=source.title,
                scheme_id=source.scheme_id,
                page_type=source.page_type,
                section_path=list(draft.section_path),
                field=draft.field,
                chunk_type=draft.chunk_type,
                content_class=draft.content_class,
                indexed=indexed,
                exclusion_reason=reason,
                text=text,
                token_count=token_count,
                content_hash=_content_hash(text),
                fetched_at=source.fetched_at,
                oversize=oversize,
            )
        )

    return chunks


def validate_chunks(
    chunks: list[Chunk],
    *,
    settings: Settings,
    token_counter: TokenCounter | None = None,
) -> list[str]:
    """Check the §4.6 invariants and return human-readable violations."""
    counter = token_counter or TokenCounter(settings.chunking.tokenizer)
    max_tokens = settings.chunking.max_tokens
    exclude_classes = set(settings.chunking.exclude_content_classes)
    violations: list[str] = []

    seen_ids: set[str] = set()
    for chunk in chunks:
        if chunk.chunk_id in seen_ids:
            violations.append(f"duplicate chunk_id {chunk.chunk_id}")
        seen_ids.add(chunk.chunk_id)

        if chunk.token_count != counter.count(chunk.text):
            violations.append(
                f"{chunk.chunk_id}: stored token_count does not match text"
            )
        if chunk.indexed and chunk.token_count > max_tokens:
            violations.append(
                f"{chunk.chunk_id}: indexed chunk has {chunk.token_count} tokens "
                f"(limit {max_tokens})"
            )
        if chunk.indexed and chunk.oversize:
            violations.append(f"{chunk.chunk_id}: oversize chunk is indexed")
        if chunk.indexed and chunk.content_class in exclude_classes:
            violations.append(
                f"{chunk.chunk_id}: {chunk.content_class} chunk is indexed"
            )
        if not chunk.indexed and not chunk.exclusion_reason:
            violations.append(f"{chunk.chunk_id}: excluded chunk has no reason")
        if not chunk.text.strip():
            violations.append(f"{chunk.chunk_id}: empty text")

    return violations
