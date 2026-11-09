"""Rendering-safety tests for the Streamlit UI."""

from __future__ import annotations

from pathlib import Path

from mf_faq.config import Settings

from tests.ui_test_utils import (
    CONFIG,
    ELSS_URL,
    LARGE_CAP_URL,
    FakeAnswerService,
    all_ui_text,
    answered_response,
    fake_store,
    pii_response,
    run_app,
)


def test_html_in_answer_is_escaped() -> None:
    answer = "<script>alert('x')</script> is not a fact."
    service = FakeAnswerService(
        [answered_response(answer=answer, trace_id="4" * 32)]
    )
    app = run_app(service, store=fake_store())

    app.chat_input[0].set_value(
        "What is the expense ratio of HDFC ELSS Tax Saver Fund?"
    ).run()

    assert not app.exception
    markdown_values = "\n".join(str(item.value) for item in app.markdown)
    assert "<script>alert('x')</script>" not in markdown_values
    assert "&lt;script&gt;alert('x')&lt;/script&gt;" in markdown_values
    assert len(app.html) == 0


def test_off_allowlist_citation_is_not_rendered_as_a_link() -> None:
    service = FakeAnswerService(
        [
            answered_response(
                citation_url="https://evil.example/invented",
                citation_title="Invented source",
                trace_id="5" * 32,
            )
        ]
    )
    app = run_app(service, store=fake_store())

    app.chat_input[0].set_value(
        "What is the expense ratio of HDFC ELSS Tax Saver Fund?"
    ).run()

    assert not app.exception
    urls = [button.url for button in app.link_button]
    assert "https://evil.example/invented" not in urls
    assert not [
        button for button in app.link_button if button.label.startswith("Source:")
    ]
    assert "Citation unavailable." in all_ui_text(app)


def test_allowlisted_citation_outside_evidence_is_rejected() -> None:
    service = FakeAnswerService(
        [
            answered_response(
                citation_url=LARGE_CAP_URL,
                citation_title="HDFC Large Cap Fund Direct Growth",
                trace_id="6" * 32,
            )
        ]
    )
    app = run_app(service, store=fake_store())

    app.chat_input[0].set_value(
        "What is the expense ratio of HDFC ELSS Tax Saver Fund?"
    ).run()

    source_urls = [
        button.url
        for button in app.link_button
        if button.label.startswith("Source:")
    ]
    assert ELSS_URL not in source_urls
    assert LARGE_CAP_URL not in source_urls
    assert "Citation unavailable." in all_ui_text(app)


def test_no_secret_appears_in_the_dom(monkeypatch) -> None:
    secret = "sk-ui-secret-1234567890"
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("LLM_MODEL", "test-model")
    monkeypatch.setenv("GROQ_API_KEY", secret)
    settings = Settings.load(CONFIG)
    assert settings.provider_key_present()

    app = run_app(FakeAnswerService([]), settings=settings, store=fake_store())

    text = all_ui_text(app)
    assert secret not in text
    assert "key: present" in text
    assert "test-model" in text


def test_pii_is_not_echoed_in_the_dom() -> None:
    raw = "My PAN is YYYYY8888Y and my phone is 9876501234"
    service = FakeAnswerService([pii_response(trace_id="7" * 32)])
    app = run_app(service, store=fake_store())

    app.chat_input[0].set_value(raw).run()

    text = all_ui_text(app)
    assert "YYYYY8888Y" not in text
    assert "9876501234" not in text
    assert app.session_state["messages"][1]["query"] == ""


def test_streamlit_binds_to_localhost_only() -> None:
    root = Path(__file__).resolve().parents[2]
    config = root / ".streamlit" / "config.toml"
    assert config.is_file()
    text = config.read_text(encoding="utf-8")
    assert 'address = "localhost"' in text
    assert "0.0.0.0" not in text


def test_ui_sources_do_not_enable_raw_html() -> None:
    root = Path(__file__).resolve().parents[2] / "src" / "mf_faq" / "ui"
    for name in ("app.py", "cards.py", "inspector.py", "state.py"):
        source = (root / name).read_text(encoding="utf-8")
        assert "unsafe_allow_html=True" not in source
    cards = (root / "cards.py").read_text(encoding="utf-8")
    app_source = (root / "app.py").read_text(encoding="utf-8")
    assert "unsafe_allow_html=False" in cards
    assert "unsafe_allow_html=False" in app_source
