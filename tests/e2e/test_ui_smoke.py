"""Ask-tab UI smoke tests with a fake answer service."""

from __future__ import annotations

from mf_faq.ui import state as ui_state

from tests.ui_test_utils import (
    CLARIFY_OPTIONS,
    ELSS_TITLE,
    ELSS_URL,
    FakeAnswerService,
    all_ui_text,
    answered_response,
    clarify_response,
    fake_store,
    pii_response,
    run_app,
    write_inspector_fixtures,
)

assert CLARIFY_OPTIONS  # imported for readability of the clarify assertion


def test_initial_state_shows_title_examples_and_disclaimer() -> None:
    app = run_app(FakeAnswerService([]), store=fake_store())

    assert not app.exception
    assert app.title[0].value == "HDFC MF Facts Assistant"
    labels = [button.label for button in app.button]
    for example in ui_state.EXAMPLES:
        assert example in labels
    assert len(ui_state.EXAMPLES) == 3

    text = all_ui_text(app)
    assert "Facts-only. No investment advice." in text
    assert ui_state.WELCOME in text
    assert "Sources indexed: 5 pages" in text
    assert "SEBI-registered investment adviser" in text
    assert "Do not enter PAN, Aadhaar" in text


def test_example_chip_submits_a_question() -> None:
    service = FakeAnswerService([answered_response()])
    app = run_app(service, store=fake_store())

    app.button(key="example_0").click().run()

    assert not app.exception
    assert len(service.calls) == 1
    assert service.calls[0].query == ui_state.EXAMPLES[0]
    assert "Answered" in all_ui_text(app)


def test_answered_card_has_one_citation_and_freshness() -> None:
    service = FakeAnswerService([answered_response()])
    app = run_app(service, store=fake_store())

    app.chat_input[0].set_value(
        "What is the expense ratio of HDFC ELSS Tax Saver Fund?"
    ).run()

    assert not app.exception
    citations = [
        button for button in app.link_button if button.label.startswith("Source:")
    ]
    assert len(citations) == 1
    assert citations[0].url == ELSS_URL
    assert "Last updated from sources: 2026-11-09" in all_ui_text(app)
    assert "The expense ratio is 1.20%." in all_ui_text(app)
    assert "Trace aaaaaaaa" in all_ui_text(app)
    # Scheme memory is updated from the resolved question.
    assert app.session_state["last_scheme"] == "hdfc_elss"
    assert app.session_state["turn_count"] == 0


def test_follow_up_uses_scheme_memory() -> None:
    service = FakeAnswerService(
        [answered_response(trace_id="1" * 32), answered_response(trace_id="2" * 32)]
    )
    app = run_app(service, store=fake_store())

    app.chat_input[0].set_value(
        "What is the expense ratio of HDFC ELSS Tax Saver Fund?"
    ).run()
    app.chat_input[0].set_value("What about the exit load?").run()

    assert len(service.calls) == 2
    assert service.calls[1].last_scheme == "hdfc_elss"
    assert service.calls[1].turn_count == 0
    assert app.session_state["turn_count"] == 1


def test_clarify_card_shows_four_scheme_buttons_and_resubmits() -> None:
    service = FakeAnswerService(
        [clarify_response(), answered_response(trace_id="3" * 32)]
    )
    app = run_app(service, store=fake_store())

    app.chat_input[0].set_value("What is the expense ratio?").run()

    labels = [button.label for button in app.button]
    for option in CLARIFY_OPTIONS:
        assert option["title"] in labels

    chosen = next(
        button for button in app.button if button.label == ELSS_TITLE
    )
    chosen.click().run()

    assert not app.exception
    assert len(service.calls) == 2
    assert ELSS_TITLE in service.calls[1].query
    assert service.calls[1].query.lower().startswith("what is the expense ratio")
    citations = [
        button for button in app.link_button if button.label.startswith("Source:")
    ]
    assert len(citations) == 1
    assert citations[0].url == ELSS_URL


def test_pii_card_shows_fixed_copy_and_hides_raw_query() -> None:
    service = FakeAnswerService([pii_response()])
    app = run_app(service, store=fake_store())
    raw = "My PAN is ZZZZZ9999Z and my OTP is 987654"

    app.chat_input[0].set_value(raw).run()

    assert not app.exception
    text = all_ui_text(app)
    assert "ZZZZZ9999Z" not in text
    assert "987654" not in text
    assert "can't accept PAN, Aadhaar" in text
    assert "Blocked: personal information" in text
    assert not [
        button for button in app.link_button if button.label.startswith("Source:")
    ]
    assert app.session_state["messages"][0]["text"] == (
        "Message blocked: personal information"
    )
    assert app.session_state["messages"][1]["query"] == ""


def test_evaluation_tab_shows_summary_but_not_recorded_answers(
    tmp_path,
) -> None:
    settings = write_inspector_fixtures(tmp_path)
    app = run_app(
        FakeAnswerService([]), settings=settings, store=fake_store()
    )

    assert not app.exception
    text = all_ui_text(app)
    assert "Fixture evaluation table." in text
    assert "secret-answer-marker" not in text


def test_clear_conversation_resets_session_state() -> None:
    service = FakeAnswerService([answered_response()])
    app = run_app(service, store=fake_store())
    app.chat_input[0].set_value(
        "What is the expense ratio of HDFC ELSS Tax Saver Fund?"
    ).run()
    assert app.session_state["messages"]
    assert app.session_state["last_scheme"] == "hdfc_elss"

    app.button(key="clear_conversation").click().run()

    assert not app.exception
    assert app.session_state["messages"] == []
    assert app.session_state["last_scheme"] is None
    assert app.session_state["turn_count"] == 0
    assert app.session_state["last_trace_id"] is None
    assert len(app.chat_message) == 0
    assert app.title[0].value == "HDFC MF Facts Assistant"
