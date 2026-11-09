"""Chunker unit tests: atomic facts, prefixes, limits, dedupe, exclusions."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from mf_faq.chunking.chunker import (
    TokenCounter,
    chunk_document,
    render_chunk_text,
    split_sentences,
    validate_chunks,
)
from mf_faq.config import Settings
from mf_faq.contracts import SourcePage

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "config" / "config.yaml"
NOW = datetime(2026, 11, 8, 10, 0, tzinfo=timezone.utc)
TITLE = "HDFC ELSS Tax Saver Fund Direct Plan Growth"


def word_counter(text: str) -> int:
    return len(text.split())


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    loaded = Settings.load(CONFIG, base_dir=tmp_path)
    loaded.chunking = loaded.chunking.model_copy(
        update={"max_tokens": 60, "min_tokens": 0}
    )
    return loaded


@pytest.fixture()
def counter() -> TokenCounter:
    return TokenCounter("test", count_fn=word_counter)


def source(title: str = TITLE, doc_id: str = "c4_hdfc_elss") -> SourcePage:
    return SourcePage(
        doc_id=doc_id,
        url=f"https://groww.in/mutual-funds/{doc_id}",
        title=title,
        page_type="scheme",
        scheme_id="hdfc_elss",
        fetched_at=NOW,
        http_status=200,
        content_hash="sha256:" + "a" * 64,
        text="fixture corpus",
    )


def kv(label: str, value: str, section: list[str] | None = None) -> dict:
    path = [TITLE, *(section or ["Fund details"])]
    return {
        "kind": "kv_pair",
        "text": f"{label}: {value}",
        "label": label,
        "value": value,
        "section_path": path,
        "header": None,
    }


def paragraph(text: str, section: list[str] | None = None) -> dict:
    return {
        "kind": "paragraph",
        "text": text,
        "label": None,
        "value": None,
        "section_path": [TITLE, *(section or ["About the fund"])],
        "header": None,
    }


def test_kv_pair_stays_intact(settings: Settings, counter: TokenCounter) -> None:
    long_value = " ".join(f"word{i}" for i in range(120))
    chunks = chunk_document(
        source(), [kv("Exit load", long_value)], settings, token_counter=counter
    )

    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk.chunk_type == "kv_pair"
    assert long_value in chunk.text
    assert chunk.oversize is True
    assert chunk.indexed is False
    assert chunk.exclusion_reason


def test_table_row_stays_intact_and_includes_headers(
    settings: Settings, counter: TokenCounter
) -> None:
    row = {
        "kind": "table_row",
        "text": "Parameter: Exit load; Value: 1% within 1 year",
        "label": None,
        "value": None,
        "section_path": [TITLE, "Fund details"],
        "header": ["Parameter", "Value"],
    }
    chunks = chunk_document(source(), [row], settings, token_counter=counter)

    assert len(chunks) == 1
    assert chunks[0].chunk_type == "table_row"
    assert "Parameter: Exit load" in chunks[0].text
    assert "Value: 1% within 1 year" in chunks[0].text


def test_section_prefix_present(settings: Settings, counter: TokenCounter) -> None:
    chunks = chunk_document(
        source(), [kv("Expense ratio", "1.20%")], settings, token_counter=counter
    )

    assert chunks[0].text == (
        f"{TITLE} > Fund details > Expense ratio: 1.20%"
    )
    assert chunks[0].section_path == [TITLE, "Fund details"]


def test_prefix_is_not_duplicated(settings: Settings, counter: TokenCounter) -> None:
    block = kv("Expense ratio", "1.20%", section=[])
    block["section_path"] = [TITLE]
    chunks = chunk_document(source(), [block], settings, token_counter=counter)

    assert chunks[0].text == f"{TITLE} > Expense ratio: 1.20%"
    assert chunks[0].text.count(TITLE) == 1


def test_token_limit_enforced(settings: Settings, counter: TokenCounter) -> None:
    settings.chunking = settings.chunking.model_copy(update={"max_tokens": 40})
    long_paragraph = (
        "The scheme invests primarily in equity and equity related instruments. "
        "It follows a tax saving mandate with a statutory lock in period. "
        "The fund may also invest in debt and money market instruments. "
        "It is suitable for investors seeking long term capital appreciation."
    )
    chunks = chunk_document(
        source(), [paragraph(long_paragraph)], settings, token_counter=counter
    )

    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.indexed
        assert chunk.token_count <= settings.chunking.max_tokens
        assert chunk.token_count == word_counter(chunk.text)


def test_deterministic_chunk_id(settings: Settings) -> None:
    blocks = [kv("Expense ratio", "1.20%"), kv("Exit load", "1% within 1 year")]
    first = chunk_document(
        source(), blocks, settings, token_counter=TokenCounter("test", count_fn=word_counter)
    )
    second = chunk_document(
        source(), blocks, settings, token_counter=TokenCounter("test", count_fn=word_counter)
    )

    assert [chunk.chunk_id for chunk in first] == [chunk.chunk_id for chunk in second]
    assert len({chunk.chunk_id for chunk in first}) == len(first)


def test_exact_duplicates_removed(settings: Settings, counter: TokenCounter) -> None:
    duplicate = kv("Expense ratio", "1.20%")
    chunks = chunk_document(
        source(), [duplicate, dict(duplicate)], settings, token_counter=counter
    )

    assert len(chunks) == 1


def test_performance_content_excluded(settings: Settings, counter: TokenCounter) -> None:
    return_row = {
        "kind": "table_row",
        "text": (
            "Over the past: 1 year; Total investment: ₹60,000; "
            "Would've become: ₹62,049; Returns: +3.42%"
        ),
        "label": None,
        "value": None,
        "section_path": [TITLE, "Return calculator"],
        "header": ["Over the past", "Total investment", "Would've become", "Returns"],
    }
    nav = kv("NAV", "06 Nov '26: ₹1,536.53", section=["Fund details"])
    chunks = chunk_document(
        source(), [return_row, nav], settings, token_counter=counter
    )

    assert len(chunks) == 2
    returns, nav_chunk = chunks
    assert returns.content_class == "performance"
    assert returns.indexed is False
    assert returns.exclusion_reason
    assert nav_chunk.content_class == "navigation"
    assert nav_chunk.indexed is False
    assert nav_chunk.exclusion_reason


def test_adjacent_small_paragraphs_merge_only_within_section(
    settings: Settings, counter: TokenCounter
) -> None:
    settings.chunking = settings.chunking.model_copy(update={"min_tokens": 40})
    first = paragraph("The scheme invests in equity instruments.")
    second = paragraph("The scheme may invest in debt instruments.")
    other_section = paragraph(
        "The scheme is managed by a team of experienced professionals.",
        section=["Fund management"],
    )

    chunks = chunk_document(
        source(), [first, second, other_section], settings, token_counter=counter
    )

    factual = [chunk for chunk in chunks if chunk.content_class == "factual"]
    assert len(factual) == 1
    assert "The scheme invests in equity instruments." in factual[0].text
    assert "The scheme may invest in debt instruments." in factual[0].text


def test_invariants_hold_for_mixed_blocks(
    settings: Settings, counter: TokenCounter
) -> None:
    blocks = [
        kv("Expense ratio", "1.20%"),
        paragraph("The scheme invests in equity and equity related instruments."),
        {
            "kind": "table_row",
            "text": "Over the past: 1 year; Returns: +3.42%",
            "label": None,
            "value": None,
            "section_path": [TITLE, "Return calculator"],
            "header": ["Over the past", "Returns"],
        },
    ]
    chunks = chunk_document(source(), blocks, settings, token_counter=counter)

    assert validate_chunks(chunks, settings=settings, token_counter=counter) == []


def test_other_kv_pair_keeps_its_label_as_context(
    settings: Settings, counter: TokenCounter
) -> None:
    block = kv("No of Schemes", "84", section=[])
    chunks = chunk_document(source(), [block], settings, token_counter=counter)

    assert chunks[0].field == "other"
    assert chunks[0].text == f"{TITLE} > Fund details > No of Schemes: 84"
    assert "Fact:" not in chunks[0].text


def test_kv_label_with_embedded_value_is_rendered_once(
    settings: Settings, counter: TokenCounter
) -> None:
    block = kv(
        "Stamp duty on investment: 0.005% (from July 1st, 2020)",
        "from July 1st 2020",
    )
    chunks = chunk_document(source(), [block], settings, token_counter=counter)

    assert chunks[0].text.endswith(
        "Stamp duty on investment: 0.005% (from July 1st, 2020)"
    )
    assert chunks[0].text.count("Stamp duty on investment") == 1


def test_long_prose_does_not_inherit_field_from_section_title(
    settings: Settings, counter: TokenCounter
) -> None:
    block = paragraph(
        "This scheme follows a disciplined process across market capitalisations "
        "and aims to build a diversified portfolio for long term investors.",
        section=["About HDFC Flexi Cap Direct Plan Growth"],
    )
    chunks = chunk_document(source(), [block], settings, token_counter=counter)

    assert chunks[0].field == "other"
    assert chunks[0].indexed


def test_split_sentences_keeps_common_abbreviations() -> None:
    text = "The fund is managed by Mr. Example. It invests in equity. Returns vary."
    assert split_sentences(text) == [
        "The fund is managed by Mr. Example.",
        "It invests in equity.",
        "Returns vary.",
    ]


def test_render_strips_duplicate_field_tail() -> None:
    from mf_faq.chunking.chunker import _Draft

    draft = _Draft(
        section_path=[TITLE, "Fund details", "Expense ratio"],
        field="expense_ratio",
        chunk_type="kv_pair",
        content_class="factual",
        body="1.20%",
    )
    assert render_chunk_text(TITLE, draft) == (
        f"{TITLE} > Fund details > Expense ratio: 1.20%"
    )
