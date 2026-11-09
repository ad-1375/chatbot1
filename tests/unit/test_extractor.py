"""Extractor tests: atomic blocks, section paths, noise, scrubbing."""

from __future__ import annotations

import pytest

from mf_faq.ingest.extract import (
    ExtractionError,
    detect_coverage,
    extract_document,
    scrub_contact_details,
)

FILLER = (
    "<p>HDFC Mutual Fund is an asset management company that offers equity, "
    "debt and hybrid schemes to investors across India.</p>"
)


def html(body: str, *, title: str = "Test Scheme - Groww") -> str:
    return (
        "<!DOCTYPE html><html><head><title>"
        + title
        + "</title></head><body><div class='container'><div class='layout-main width100'>"
        + body
        + "</div></div></body></html>"
    )


def test_definition_list_pair_becomes_one_kv_pair() -> None:
    document = extract_document(
        html(
            "<h1>HDFC ELSS Tax Saver Fund Direct Plan Growth</h1>"
            "<h2>Fund details</h2>"
            "<dl><dt>Expense ratio</dt><dd>1.20%</dd></dl>"
            + FILLER
        )
    )

    pairs = [block for block in document.blocks if block.kind == "kv_pair"]
    assert len(pairs) == 1
    assert pairs[0].label == "Expense ratio"
    assert pairs[0].value == "1.20%"
    assert pairs[0].text == "Expense ratio: 1.20%"


def test_table_row_keeps_header_names_and_values() -> None:
    document = extract_document(
        html(
            "<h1>Scheme</h1><h2>Key information</h2>"
            "<table>"
            "<tr><th>Field</th><th>Value</th></tr>"
            "<tr><td>Exit load</td><td>1% within 1 year</td></tr>"
            "</table>"
            + FILLER
        )
    )

    rows = [block for block in document.blocks if block.kind == "table_row"]
    assert len(rows) == 1
    assert rows[0].header == ["Field", "Value"]
    assert "Field: Exit load" in rows[0].text
    assert "Value: 1% within 1 year" in rows[0].text


def test_two_column_table_becomes_kv_pairs() -> None:
    document = extract_document(
        html(
            "<h1>Scheme</h1><h2>Key information</h2>"
            "<table>"
            "<tr><td>Launch Date</td><td>10 Dec 1999</td></tr>"
            "<tr><td>Total AUM</td><td>₹9,34,075.66 Cr</td></tr>"
            "</table>"
            + FILLER
        )
    )

    pairs = [block for block in document.blocks if block.kind == "kv_pair"]
    assert [(pair.label, pair.value) for pair in pairs] == [
        ("Launch Date", "10 Dec 1999"),
        ("Total AUM", "₹9,34,075.66 Cr"),
    ]


def test_nested_headings_produce_correct_section_path() -> None:
    document = extract_document(
        html(
            "<h1>HDFC ELSS Tax Saver Fund Direct Plan Growth</h1>"
            "<h2>Fund details</h2>"
            "<h3>Expense ratio</h3>"
            "<div class='flex flex-column pair'><div>Expense ratio</div><div>1.20%</div></div>"
            + FILLER
        )
    )

    pair = next(block for block in document.blocks if block.kind == "kv_pair")
    assert pair.section_path == [
        "HDFC ELSS Tax Saver Fund Direct Plan Growth",
        "Fund details",
        "Expense ratio",
    ]


def test_section_heading_does_not_leak_into_later_siblings() -> None:
    document = extract_document(
        html(
            "<h1>Scheme</h1>"
            "<section><h2>Holdings</h2><p>Holding rows are performance content.</p></section>"
            "<section><h3>Exit load</h3>"
            "<div class='flex flex-column pair'><div>Exit load</div><div>Nil</div></div>"
            "</section>"
            + FILLER
        )
    )

    pair = next(block for block in document.blocks if block.kind == "kv_pair")
    assert pair.section_path == ["Scheme", "Exit load"]


def test_deepest_kv_pair_is_preferred_over_outer_wrapper() -> None:
    document = extract_document(
        html(
            "<h1>Scheme</h1><h2>About Scheme</h2>"
            "<div class='content-section'>"
            "<div class='benchmark-row'>"
            "<span>Fund benchmark</span><span>NIFTY 500 Total Return Index</span>"
            "</div>"
            "<span>Scheme Information Document(SID)</span>"
            "</div>"
            + FILLER
        )
    )

    pairs = [block for block in document.blocks if block.kind == "kv_pair"]
    assert [(pair.label, pair.value) for pair in pairs] == [
        ("Fund benchmark", "NIFTY 500 Total Return Index")
    ]


def test_heading_wrapper_sets_section_for_following_siblings() -> None:
    document = extract_document(
        html(
            "<h1>Scheme</h1>"
            "<div class='heading-wrapper'><h3>Expense ratio</h3></div>"
            "<div class='flex flex-column pair'><div>Expense ratio</div><div>1.20%</div></div>"
            + FILLER
        )
    )

    pair = next(block for block in document.blocks if block.kind == "kv_pair")
    assert pair.section_path == ["Scheme", "Expense ratio"]


def test_nav_footer_cookie_and_script_are_removed() -> None:
    document = extract_document(
        html(
            "<nav>NAVNOISE menu</nav>"
            "<footer>FOOTERNOISE legal</footer>"
            "<div class='cookie-banner'>COOKIENOISE consent</div>"
            "<script>SCRIPTNOISE alert(1)</script>"
            "<h1>Scheme</h1><h2>Fund details</h2>"
            "<dl><dt>Expense ratio</dt><dd>1.20%</dd></dl>"
            + FILLER
        )
    )

    assert "NAVNOISE" not in document.text
    assert "FOOTERNOISE" not in document.text
    assert "COOKIENOISE" not in document.text
    assert "SCRIPTNOISE" not in document.text
    assert "Expense ratio: 1.20%" in document.text


@pytest.mark.parametrize(
    "value",
    ["1.20%", "₹500", "0.005%", "10 Dec 1999", "₹9,34,075.66 Cr", "#2 in India"],
)
def test_numeric_values_are_preserved_exactly(value: str) -> None:
    document = extract_document(
        html(
            "<h1>Scheme</h1><h2>Fund details</h2>"
            f"<dl><dt>Value</dt><dd>{value}</dd></dl>"
            + FILLER
        )
    )
    assert value in document.text


def test_corpus_email_and_phone_are_scrubbed() -> None:
    document = extract_document(
        html(
            "<h1>Scheme</h1><h2>Fund house</h2>"
            "<p>E-mail investor@example.com and phone 9876543210 or +91 98765 43210.</p>"
            "<p>Minimum SIP is ₹500 and the expense ratio is 1.20% on 10 Dec 1999.</p>"
            + FILLER
        )
    )

    assert "investor@example.com" not in document.text
    assert "9876543210" not in document.text
    assert "98765 43210" not in document.text
    assert "₹500" in document.text
    assert "1.20%" in document.text
    assert "10 Dec 1999" in document.text
    assert "[email removed]" in document.text
    assert "[phone removed]" in document.text


def test_scrub_does_not_touch_amounts_dates_or_codes() -> None:
    text = (
        "Expense ratio 0.75% on 08 Nov 2026, minimum SIP ₹500, section 80C, "
        "code HDFCELSS, SEBI registration code PM /INP000000506"
    )
    assert scrub_contact_details(text) == text


def test_landline_with_spaced_dash_is_scrubbed() -> None:
    text = "Phone: 022 – 66316333 E-mail: investor@example.com"
    scrubbed = scrub_contact_details(text)

    assert "66316333" not in scrubbed
    assert "investor@example.com" not in scrubbed
    assert "[phone removed]" in scrubbed
    assert "[email removed]" in scrubbed


def test_coverage_detects_required_facts() -> None:
    text = (
        "Expense ratio 1.20%. Exit load Nil. Minimum SIP is ₹500. "
        "ELSS has a 3Y lock-in. Riskometer: Very High Risk. "
        "Benchmark: NIFTY 500 Total Return Index. "
        "Account statement download is available."
    )
    coverage = detect_coverage(text)

    assert all(coverage.values()), coverage


def test_empty_content_raises() -> None:
    with pytest.raises(ExtractionError):
        extract_document("<html><body><div class='layout-main'></div></body></html>")
