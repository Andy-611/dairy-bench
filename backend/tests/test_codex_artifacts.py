"""Tests for exporting public Codex Session fields."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from openai_codex.types import ThreadItem

from company_bench.codex_artifacts import (
    CodexArtifactIdentity,
    CodexArtifactStore,
)


def _identity() -> CodexArtifactIdentity:
    return CodexArtifactIdentity(
        invocation_id="run_test.1.farm_a",
        run_id="run_test",
        company_id="farm_a",
        day=1,
        model="gpt-5.6-terra",
        thread_id="thread_target",
        turn_id="turn_target",
    )


def _result(
    final_response: str,
    items: list[ThreadItem] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        final_response=final_response,
        items=items or [],
    )


def _write_session(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in records),
        encoding="utf-8",
    )


def test_exports_only_reasoning_summary_and_final_output(tmp_path: Path) -> None:
    session = tmp_path / "rollout-thread_target.jsonl"
    metadata = {"turn_id": "turn_target"}
    records = [
        {
            "type": "session_meta",
            "payload": {"id": "thread_target", "session_id": "thread_target"},
        },
        {
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "turn_target"},
        },
        {
            "type": "turn_context",
            "payload": {"turn_id": "turn_target", "model": "gpt-5.6-terra"},
        },
        {
            "type": "response_item",
            "payload": {
                "type": "reasoning",
                "content": [
                    {"type": "reasoning_text", "text": "Raw reasoning text"},
                ],
                "summary": [
                    {"type": "summary_text", "text": "Summary one"},
                    {"type": "summary_text", "text": "Summary two"},
                ],
                "encrypted_content": "never-export-this-secret",
                "internal_chat_message_metadata_passthrough": metadata,
            },
        },
        {
            "type": "event_msg",
            "payload": {"type": "agent_reasoning", "text": "Summary one"},
        },
        {
            "type": "event_msg",
            "payload": {"type": "agent_reasoning", "text": "Summary one"},
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "phase": "commentary",
                "content": [{"type": "output_text", "text": "Discarded commentary"}],
                "internal_chat_message_metadata_passthrough": metadata,
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "phase": "final_answer",
                "content": [{"type": "output_text", "text": '{"decision":{"kind":"farm"}}'}],
                "internal_chat_message_metadata_passthrough": metadata,
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "turn_id": "turn_target",
                "last_agent_message": '{"decision":{"kind":"farm"}}',
            },
        },
        {
            "type": "turn_context",
            "payload": {"turn_id": "turn_other", "model": "gpt-5.6-terra"},
        },
        {
            "type": "event_msg",
            "payload": {"type": "agent_reasoning", "text": "Other-turn reasoning"},
        },
    ]
    _write_session(session, records)
    store = CodexArtifactStore(tmp_path / "run_artifacts")

    exported = store.export(
        _identity(),
        _result('{"decision":{"kind":"fallback"}}'),
        session,
    )

    reasoning_path = tmp_path / "run_artifacts" / "run_test" / "reasoning" / "day-001__farm_a.md"
    output_path = tmp_path / "run_artifacts" / "run_test" / "final_outputs" / "day-001__farm_a.json"
    assert reasoning_path.is_file()
    assert output_path.is_file()
    assert exported.reasoning_markdown == reasoning_path.read_text(encoding="utf-8").rstrip()
    assert "## Reasoning Summary" in exported.reasoning_markdown
    assert "Summary one" in exported.reasoning_markdown
    assert "Summary two" in exported.reasoning_markdown
    assert exported.reasoning_markdown.count("Summary one") == 1
    assert "Raw reasoning text" not in exported.reasoning_markdown
    assert "- Session: read" in exported.reasoning_markdown
    assert "never-export-this-secret" not in exported.reasoning_markdown
    assert "Discarded commentary" not in exported.reasoning_markdown
    assert "Other-turn reasoning" not in exported.reasoning_markdown
    assert json.loads(exported.final_output) == {"decision": {"kind": "fallback"}}
    assert store.read(_identity()) == exported
    assert store.read(_identity().model_copy(update={"turn_id": "turn_other"})) is None


def test_falls_back_to_completed_sdk_items_without_a_session(tmp_path: Path) -> None:
    item = ThreadItem.model_validate(
        {
            "type": "reasoning",
            "id": "reasoning_test",
            "content": ["SDK raw reasoning"],
            "summary": ["SDK reasoning summary"],
        }
    )
    store = CodexArtifactStore(tmp_path / "run_artifacts")

    exported = store.export(
        _identity(),
        _result('{"decision":{"kind":"farm"}}', [item]),
        None,
    )

    assert "SDK raw reasoning" not in exported.reasoning_markdown
    assert "SDK reasoning summary" in exported.reasoning_markdown
    assert "- Session: unavailable" in exported.reasoning_markdown
    assert "unknown (Session unavailable or partial)" in exported.reasoning_markdown
    assert json.loads(exported.final_output) == {"decision": {"kind": "farm"}}


def test_v2_turns_use_distinct_files_within_one_company_day(tmp_path: Path) -> None:
    store = CodexArtifactStore(tmp_path / "run_artifacts")
    first = _identity().model_copy(
        update={
            "invocation_id": "run_test.farm_a.t1.provider",
            "domain_turn_id": "run_test.farm_a.t1",
            "turn_id": "provider_turn_1",
        }
    )
    second = first.model_copy(
        update={
            "invocation_id": "run_test.farm_a.t2.provider",
            "domain_turn_id": "run_test.farm_a.t2",
            "turn_id": "provider_turn_2",
        }
    )

    first_view = store.export(first, _result('{"command":{"kind":"produce"}}'), None)
    second_view = store.export(second, _result('{"command":{"kind":"wait"}}'), None)

    output_root = tmp_path / "run_artifacts" / "run_test" / "final_outputs"
    assert (output_root / "day-001__farm_a__turn-0001.json").is_file()
    assert (output_root / "day-001__farm_a__turn-0002.json").is_file()
    assert store.read(first) == first_view
    assert store.read(second) == second_view
    assert "- Domain turn: run_test.farm_a.t1" in first_view.reasoning_markdown
    assert first_view.final_output != second_view.final_output


def test_v2_retried_turn_preserves_each_physical_call(tmp_path: Path) -> None:
    store = CodexArtifactStore(tmp_path / "run_artifacts")
    first = _identity().model_copy(
        update={
            "invocation_id": "inv_first",
            "domain_turn_id": "run_test.farm_a.t1",
            "thread_id": "thread_first",
            "turn_id": "provider_turn_first",
        }
    )
    second = first.model_copy(
        update={
            "invocation_id": "inv_second",
            "thread_id": "thread_second",
            "turn_id": "provider_turn_second",
        }
    )

    first_view = store.export(first, _result('{"command":{"kind":"produce"}}'), None)
    second_view = store.export(second, _result('{"command":{"kind":"wait"}}'), None)

    output_root = tmp_path / "run_artifacts" / "run_test" / "final_outputs"
    outputs = tuple(output_root.glob("day-001__farm_a__turn-0001*.json"))
    assert len(outputs) == 2
    assert store.read(first) == first_view
    assert store.read(second) == second_view
    assert first_view.final_output != second_view.final_output


def test_uses_english_placeholder_when_summary_is_unavailable(tmp_path: Path) -> None:
    store = CodexArtifactStore(tmp_path / "run_artifacts")

    exported = store.export(
        _identity(),
        _result('{"decision":{"kind":"farm"}}'),
        None,
    )

    assert "## Reasoning Summary" in exported.reasoning_markdown
    assert "No public reasoning summary was available for this turn." in (
        exported.reasoning_markdown
    )
    assert exported.reasoning_markdown.isascii()
