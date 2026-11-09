"""Turn a fetched Groww page into cleaned text and atomic blocks.

The extractor deliberately works on the rendered DOM (not on hidden Next.js
JSON) so the audit trail is the same content a reader sees. It keeps semantic
units intact for the chunker:

* ``<dl>`` pairs and two-column tables become ``kv_pair`` blocks;
* wide tables become one ``table_row`` block per data row, with header names
  repeated in the text;
* headings only update the section path (they are never facts);
* paragraphs and list items stay whole.

Emails and phone numbers are scrubbed from the corpus before anything is
stored (implementation.md §3.3 step 5). Numbers, currency symbols, percentages
and dates are never rewritten.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterator

from selectolax.lexbor import LexborHTMLParser

try:  # trafilatura is an optional fallback at extraction time
    import trafilatura
except ImportError:  # pragma: no cover - declared dependency
    trafilatura = None  # type: ignore[assignment]


class ExtractionError(RuntimeError):
    """The page had no usable main content."""


_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_BLOCK_TAGS = {"table", "dl", "ul", "ol", "p"}
_SKIP_TAGS = {
    "script",
    "style",
    "noscript",
    "template",
    "svg",
    "iframe",
    "canvas",
    "video",
    "audio",
    "form",
    "button",
    "input",
    "select",
    "option",
    "nav",
    "footer",
    "aside",
}
_NOISE_TOKENS = (
    "cookie",
    "consent",
    "advert",
    "ad-slot",
    "promo",
    "sponsor",
    "similar-fund",
    "similarfund",
    "also-manages",
    "app-download",
    "download-app",
    "signup",
    "sign-up",
    "newsletter",
    "breadcrumb",
    "modal",
    "popup",
    "overlay",
)

_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
_OBFUSCATED_EMAIL_RE = re.compile(r"\[email\s*(?:&#160;|\s)*protected\]", re.IGNORECASE)
_MOBILE_RE = re.compile(r"(?<!\d)(?:\+?91[\s\-]?)?[6-9]\d{4}[\s\-]?\d{5}(?!\d)")
_LANDLINE_RE = re.compile(r"(?<!\d)0[1-9]\d{1,3}[\s\-–]*\d{6,8}(?!\d)")
_RISK_RE = re.compile(
    r"\b(very high|moderately high|high|moderate|moderately low|low)\s+risk\b",
    re.IGNORECASE,
)
_WHITESPACE_RE = re.compile(r"\s+")
_TITLE_SUFFIX_RE = re.compile(r"\s*[-|]\s*(?:Groww|NAV,).*$", re.IGNORECASE)

_COVERAGE_PATTERNS: dict[str, tuple[str, ...]] = {
    "expense_ratio": ("expense ratio", "expense_ratio"),
    "exit_load": ("exit load", "exit_load"),
    "minimum_sip": ("minimum sip", "min. for sip", "min sip", "minimum sip investment"),
    "lock_in": ("lock-in", "lock in", "lockin", "lock-in period"),
    "riskometer": ("riskometer", "risk-o-meter"),
    "benchmark": ("benchmark",),
    "statement_download": (
        "statement download",
        "download statement",
        "account statement",
        "capital gains statement",
        "capital-gains statement",
    ),
}

_MIN_CONTENT_WORDS = 25


@dataclass
class Block:
    """One atomic unit kept for the chunker."""

    kind: str  # heading | paragraph | kv_pair | table_row | list_item
    text: str
    section_path: list[str] = field(default_factory=list)
    label: str | None = None
    value: str | None = None
    header: list[str] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "text": self.text,
            "section_path": list(self.section_path),
            "label": self.label,
            "value": self.value,
            "header": list(self.header) if self.header else None,
        }


@dataclass
class ExtractedDocument:
    title: str
    text: str
    blocks: list[Block]
    coverage: dict[str, bool]
    renderer: str = "httpx"
    warnings: list[str] = field(default_factory=list)

    @property
    def word_count(self) -> int:
        return len(self.text.split())


def _clean(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = text.replace("\xa0", " ").replace("\u200b", "")
    return _WHITESPACE_RE.sub(" ", text).strip()


def scrub_contact_details(text: str) -> str:
    """Remove corpus emails/phones without touching amounts, dates or ratios."""
    text = _OBFUSCATED_EMAIL_RE.sub("[email removed]", text)
    text = _EMAIL_RE.sub("[email removed]", text)
    text = _MOBILE_RE.sub("[phone removed]", text)
    text = _LANDLINE_RE.sub("[phone removed]", text)
    return text


def detect_coverage(text: str) -> dict[str, bool]:
    lowered = text.lower()
    coverage = {
        fact: any(pattern in lowered for pattern in patterns)
        for fact, patterns in _COVERAGE_PATTERNS.items()
    }
    coverage["riskometer"] = bool(coverage["riskometer"] or _RISK_RE.search(text))
    return coverage


def _child_elements(node: Any) -> Iterator[Any]:
    child = node.child
    while child is not None:
        next_child = child.next
        if child.is_element_node:
            yield child
        child = next_child


def _is_noise(node: Any) -> bool:
    attributes = node.attributes
    if attributes.get("hidden") is not None:
        return True
    if (attributes.get("aria-hidden") or "").lower() == "true":
        return True
    style = (attributes.get("style") or "").lower().replace(" ", "")
    if "display:none" in style or "visibility:hidden" in style:
        return True
    tokens = f"{attributes.get('class', '')} {attributes.get('id', '')}".lower()
    return any(token in tokens for token in _NOISE_TOKENS)


def _remove_noise(node: Any) -> None:
    for child in list(_child_elements(node)):
        if child.tag in _SKIP_TAGS or _is_noise(child):
            child.decompose()
        else:
            _remove_noise(child)


def _choose_main(tree: LexborHTMLParser) -> Any:
    candidates = tree.css("div.layout-main")
    if candidates:
        return max(candidates, key=lambda node: len(node.text()))
    return tree.body or tree.root


def _section_path(stack: list[tuple[int, str]]) -> list[str]:
    return [title for _, title in stack]


def _push_heading(stack: list[tuple[int, str]], level: int, title: str) -> None:
    while stack and stack[-1][0] >= level:
        stack.pop()
    stack.append((level, title))


def _value_only_descendants(node: Any) -> bool:
    forbidden = {"h1", "h2", "h3", "table", "dl", "ul", "ol"}
    return any(item.tag in forbidden for item in node.iter(include_text=False))


def _looks_like_pair(node: Any) -> bool:
    """True when ``node`` itself looks like a two-cell label/value wrapper."""
    if node.tag != "div":
        return False
    elements = list(_child_elements(node))
    if len(elements) != 2:
        return False
    if _value_only_descendants(elements[0]) or _value_only_descendants(elements[1]):
        return False
    return bool(_clean(elements[0].text())) and bool(_clean(elements[1].text()))


def _as_kv_pair(node: Any) -> tuple[str, str] | None:
    """Detect a two-cell label/value container in Groww's div-based layout."""
    if node.tag != "div":
        return None
    elements = list(_child_elements(node))
    if len(elements) != 2:
        return None

    label_node, value_node = elements
    if label_node.tag in {"h1", "h2", "h3"}:
        return None
    if _value_only_descendants(label_node) or _value_only_descendants(value_node):
        return None
    # Prefer the deepest pair: skip an outer wrapper when one of its halves is
    # itself a label/value row (e.g. benchmark row + SID link).
    if _looks_like_pair(label_node) or _looks_like_pair(value_node):
        return None

    label = _clean(label_node.text()).rstrip(":").strip()
    value = _clean(value_node.text())
    if not label or not value:
        return None
    if len(label) > 80 or len(value) > 240 or len(f"{label} {value}") > 300:
        return None
    # Avoid treating a whole layout wrapper as a pair.
    if len(list(_child_elements(value_node))) > 3:
        return None
    return label, value


def _table_blocks(table: Any, section_path: list[str]) -> list[Block]:
    rows: list[list[Any]] = []
    for row in table.css("tr"):
        cells = [cell for cell in _child_elements(row) if cell.tag in {"td", "th"}]
        if cells:
            rows.append(cells)
    if not rows:
        return []

    blocks: list[Block] = []

    # Two-column tables on Groww are label/value tables (key information).
    if all(len(row) == 2 for row in rows) and not all(
        cell.tag == "th" for cell in rows[0]
    ):
        for label_cell, value_cell in rows:
            label = _clean(label_cell.text())
            value = _clean(value_cell.text())
            if label and value:
                blocks.append(
                    Block(
                        kind="kv_pair",
                        text=f"{label}: {value}",
                        section_path=list(section_path),
                        label=label,
                        value=value,
                    )
                )
        return blocks

    header: list[str] | None = None
    data_rows = rows
    if all(cell.tag == "th" for cell in rows[0]):
        header = [_clean(cell.text()) for cell in rows[0]]
        data_rows = rows[1:]

    for row in data_rows:
        values = [_clean(cell.text()) for cell in row]
        if not any(values):
            continue
        if header and len(header) == len(values):
            text = "; ".join(
                f"{name}: {value}" for name, value in zip(header, values) if name and value
            )
        else:
            text = " | ".join(value for value in values if value)
        if text:
            blocks.append(
                Block(
                    kind="table_row",
                    text=text,
                    section_path=list(section_path),
                    header=list(header) if header else None,
                )
            )
    return blocks


def _definition_blocks(definition_list: Any, section_path: list[str]) -> list[Block]:
    blocks: list[Block] = []
    pending_label: str | None = None
    for child in _child_elements(definition_list):
        if child.tag == "dt":
            pending_label = _clean(child.text())
        elif child.tag == "dd" and pending_label:
            value = _clean(child.text())
            if value:
                blocks.append(
                    Block(
                        kind="kv_pair",
                        text=f"{pending_label}: {value}",
                        section_path=list(section_path),
                        label=pending_label,
                        value=value,
                    )
                )
            pending_label = None
    return blocks


def _list_blocks(list_node: Any, section_path: list[str]) -> list[Block]:
    blocks: list[Block] = []
    for child in _child_elements(list_node):
        if child.tag != "li":
            continue
        text = _clean(child.text())
        if text:
            blocks.append(Block(kind="list_item", text=text, section_path=list(section_path)))
    return blocks


def _walk(node: Any, stack: list[tuple[int, str]], blocks: list[Block]) -> None:
    for child in _child_elements(node):
        tag = child.tag.lower()
        if tag in _SKIP_TAGS or _is_noise(child):
            continue

        # A <section>/<article> is a self-contained content block: headings
        # inside it must not leak into later siblings (for example the
        # "Holdings" heading on a Groww scheme page).
        if tag in {"section", "article"}:
            saved = list(stack)
            _walk(child, stack, blocks)
            stack[:] = saved
            continue

        if tag in _HEADING_TAGS:
            title = _clean(child.text())
            if title:
                _push_heading(stack, int(tag[1]), title)
                blocks.append(
                    Block(kind="heading", text=title, section_path=_section_path(stack))
                )
            continue

        if tag == "table":
            blocks.extend(_table_blocks(child, _section_path(stack)))
            continue

        if tag == "dl":
            blocks.extend(_definition_blocks(child, _section_path(stack)))
            continue

        if tag in {"ul", "ol"}:
            blocks.extend(_list_blocks(child, _section_path(stack)))
            continue

        if tag == "p":
            text = _clean(child.text())
            if text:
                blocks.append(
                    Block(kind="paragraph", text=text, section_path=_section_path(stack))
                )
            continue

        pair = _as_kv_pair(child)
        if pair:
            label, value = pair
            blocks.append(
                Block(
                    kind="kv_pair",
                    text=f"{label}: {value}",
                    section_path=_section_path(stack),
                    label=label,
                    value=value,
                )
            )
            continue

        if not list(_child_elements(child)):
            # Groww renders many short facts as bare text divs (for example
            # "ELSS • 3Y Lock-in" or "Very High Risk"); keep them as paragraphs.
            text = _clean(child.text())
            if text:
                blocks.append(
                    Block(kind="paragraph", text=text, section_path=_section_path(stack))
                )
            continue

        _walk(child, stack, blocks)


def blocks_to_text(blocks: list[Block]) -> str:
    return "\n".join(block.text for block in blocks if block.text).strip()


def _document_title(tree: LexborHTMLParser, fallback: str) -> str:
    heading = tree.css_first("h1")
    if heading is not None:
        title = _clean(heading.text())
        if title:
            return _TITLE_SUFFIX_RE.sub("", title)
    title_node = tree.css_first("title")
    if title_node is not None:
        title = _clean(title_node.text())
        if title:
            return _TITLE_SUFFIX_RE.sub("", title)
    return fallback


def _trafilatura_fallback(html: str) -> str:
    if trafilatura is None:
        return ""
    extracted = trafilatura.extract(
        html,
        include_tables=True,
        include_links=False,
        include_comments=False,
        favor_recall=True,
    )
    return (extracted or "").strip()


def extract_document(
    html: str,
    *,
    fallback_title: str = "",
    renderer: str = "httpx",
) -> ExtractedDocument:
    """Extract cleaned text, atomic blocks and a facts-coverage map."""
    if not html or not html.strip():
        raise ExtractionError("empty HTML")

    tree = LexborHTMLParser(html)
    title = _document_title(tree, fallback_title or "Untitled page")
    main = _choose_main(tree)
    warnings: list[str] = []

    if main is None:
        blocks: list[Block] = []
        warnings.append("no main content container; used trafilatura fallback")
    else:
        _remove_noise(main)
        # Seed the context with the page title so facts outside a <section>
        # still carry the scheme/page name in their section path.
        stack: list[tuple[int, str]] = [(1, title)] if title else []
        blocks = []
        _walk(main, stack, blocks)

    text = blocks_to_text(blocks)
    if len(text.split()) < _MIN_CONTENT_WORDS:
        fallback_text = _trafilatura_fallback(html)
        if len(fallback_text.split()) > len(text.split()):
            text = fallback_text
            blocks = [
                Block(kind="paragraph", text=_clean(line))
                for line in text.splitlines()
                if _clean(line)
            ]
            warnings.append("main content sparse; used trafilatura fallback")

    if not text.strip():
        raise ExtractionError("no usable main content")

    # Scrub corpus contact details in both the flattened text and the blocks.
    text = scrub_contact_details(text)
    for block in blocks:
        block.text = scrub_contact_details(block.text)
        if block.label is not None:
            block.label = scrub_contact_details(block.label)
        if block.value is not None:
            block.value = scrub_contact_details(block.value)

    if any(token in text for token in ("[email removed]", "[phone removed]")):
        warnings.append("corpus contact details scrubbed")

    return ExtractedDocument(
        title=title,
        text=text,
        blocks=blocks,
        coverage=detect_coverage(text),
        renderer=renderer,
        warnings=warnings,
    )
