"""Pipeline inspector tests against temporary fixture artifacts."""

from __future__ import annotations

from pathlib import Path

from mf_faq.config import Settings

from tests.ui_test_utils import (
    CONFIG,
    FakeAnswerService,
    all_ui_text,
    answered_response,
    fake_store,
    run_app,
    write_inspector_fixtures,
)


def test_all_pipeline_subtabs_load_fixture_artifacts(tmp_path: Path) -> None:
    settings = write_inspector_fixtures(tmp_path)
    app = run_app(
        FakeAnswerService([]), settings=settings, store=fake_store()
    )

    assert not app.exception
    text = all_ui_text(app)
    assert "artifact not found" not in text

    # Load
    assert "Fixture cleaned text for c1_amc_hdfc" in text
    assert "raw c1_amc_hdfc" in text
    # Chunk
    assert "elss_expense_ratio" in text
    assert "boilerplate: rating" in text
    assert "invariant violations 0" in text
    # Embed
    assert "Matrix shape (2, 384)" in text
    assert "First eight dimensions" in text
    # Index
    assert "Live collection count: 2" in text
    assert "Filter smoke results" in text
    assert "hdfc_elss" in text
    # Retrieve
    assert "Fixture question about HDFC ELSS" in text
    assert "Resolved scheme: hdfc_elss" in text
    # Answer
    assert "fixture-model" in text
    assert "fixture-hash" not in text  # hash is not rendered as a secret
    assert "SYSTEM:" in text
    assert "--- RAW RESPONSE ---" in text


def test_latest_query_appears_without_restart(tmp_path: Path) -> None:
    settings = write_inspector_fixtures(tmp_path)
    service = FakeAnswerService(
        [answered_response(trace_id="f" * 32)],
        trace_root=tmp_path / "data" / "traces",
        raw_response='{"note": "latest-raw-marker"}',
    )
    app = run_app(service, settings=settings, store=fake_store())
    query = "What is the exit load on HDFC Large Cap Fund?"

    app.chat_input[0].set_value(query).run()

    assert not app.exception
    assert app.session_state["last_trace_id"] == "f" * 32
    markdown_values = [str(item.value) for item in app.markdown]
    assert any(value.startswith("Query: ") and query in value for value in markdown_values)
    text = all_ui_text(app)
    assert "latest-raw-marker" in text
    assert "Trace " + ("f" * 8) in text


def test_missing_artifact_shows_empty_state_and_expected_path(
    tmp_path: Path,
) -> None:
    settings = Settings.load(CONFIG, base_dir=tmp_path)

    app = run_app(FakeAnswerService([]), settings=settings)

    assert not app.exception
    text = all_ui_text(app)
    assert "artifact not found" in text
    assert str(settings.resolved_path("data/manifests/load_manifest.json")) in text
    assert str(settings.resolved_path("data/chunks/chunks.jsonl")) in text
    assert str(settings.resolved_path("data/traces/answers")) in text


def test_prompt_and_raw_output_are_rendered_in_escaped_code_blocks(
    tmp_path: Path,
) -> None:
    settings = write_inspector_fixtures(tmp_path)
    app = run_app(
        FakeAnswerService([]), settings=settings, store=fake_store()
    )

    code_values = [str(item.value) for item in app.code]
    assert any("Fixture prompt" in value for value in code_values)
    assert any("--- RAW RESPONSE ---" in value for value in code_values)
    assert len(app.html) == 0
    markdown_values = "\n".join(str(item.value) for item in app.markdown)
    assert "<script>alert('prompt')</script>" not in markdown_values
    assert "<script>alert('raw')</script>" not in markdown_values
