"""Export readable Codex Session data into stable per-run artifacts."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol
from uuid import uuid4

from openai_codex import TurnResult
from pydantic import Field

from company_bench.models import CompanyId, Identifier, StrictModel

DEFAULT_ARTIFACTS_ROOT = Path(__file__).resolve().parents[3] / "run_artifacts"
_SAFE_PATH_SEGMENT = re.compile(r"^[A-Za-z0-9_.-]+$")


class CodexArtifactIdentity(StrictModel):
    """Stable coordinates for one company-day Codex turn."""

    invocation_id: Identifier
    run_id: Identifier
    company_id: CompanyId
    day: int = Field(ge=1)
    model: str = Field(min_length=1)
    thread_id: str = Field(min_length=1, max_length=128)
    turn_id: str = Field(min_length=1, max_length=128)


class CodexArtifactView(CodexArtifactIdentity):
    """Readable files returned to the local audit interface."""

    reasoning_markdown: str
    final_output: str


class CodexArtifactSink(Protocol):
    """Persistence seam used by the Codex gateway."""

    def export(
        self,
        identity: CodexArtifactIdentity,
        result: TurnResult,
        session_path: Path | None,
    ) -> CodexArtifactView:
        """Export one completed Codex turn."""
        ...


@dataclass(slots=True)
class _TurnTrace:
    """Mutable parse state kept private to one export."""

    summaries: list[str] = field(default_factory=list)
    final_output: str | None = None
    encrypted_content_present: bool | None = None
    session_status: Literal["read", "partial", "unavailable"] = "unavailable"

    def add_summary(self, text: str) -> None:
        _append_visible(self.summaries, text)


class CodexArtifactStore:
    """Read Codex JSONL once and expose two simple, durable files."""

    def __init__(
        self,
        root: Path = DEFAULT_ARTIFACTS_ROOT,
    ) -> None:
        self._root = root.expanduser().resolve()

    @classmethod
    def from_environment(cls) -> CodexArtifactStore:
        """Use the project folder unless an operator overrides it."""
        configured_root = os.getenv("DAIRY_BENCH_ARTIFACTS_DIR", "").strip()
        root = Path(configured_root) if configured_root else DEFAULT_ARTIFACTS_ROOT
        return cls(root)

    def export(
        self,
        identity: CodexArtifactIdentity,
        result: TurnResult,
        session_path: Path | None,
    ) -> CodexArtifactView:
        """Export all readable reasoning and the final structured output."""
        trace = (
            _parse_session(session_path, identity.thread_id, identity.turn_id)
            if session_path is not None and session_path.is_file()
            else _TurnTrace()
        )
        _merge_turn_items(trace, result)
        final_output = result.final_response or trace.final_output or ""
        reasoning = _render_reasoning(identity, trace)
        final_output = _format_json(final_output)

        reasoning_path, output_path = self._paths(identity)
        _write_text(reasoning_path, reasoning)
        _write_text(output_path, final_output)
        return CodexArtifactView(
            **identity.model_dump(),
            reasoning_markdown=reasoning,
            final_output=final_output,
        )

    def read(
        self,
        identity: CodexArtifactIdentity,
    ) -> CodexArtifactView | None:
        """Read an already exported turn without invoking Codex."""
        reasoning_path, output_path = self._paths(identity)
        if not reasoning_path.is_file() or not output_path.is_file():
            return None
        reasoning = reasoning_path.read_text(encoding="utf-8")
        if not _matches_identity(reasoning, identity):
            return None
        return CodexArtifactView(
            **identity.model_dump(),
            reasoning_markdown=reasoning,
            final_output=output_path.read_text(encoding="utf-8"),
        )

    def _paths(self, identity: CodexArtifactIdentity) -> tuple[Path, Path]:
        """Map one invocation to deterministic files inside the artifact root."""
        if not _SAFE_PATH_SEGMENT.fullmatch(identity.run_id):
            raise ValueError("run_id cannot be used as an artifact directory")
        stem = f"day-{identity.day:03d}__{identity.company_id}"
        run_root = self._root / identity.run_id
        return (
            run_root / "reasoning" / f"{stem}.md",
            run_root / "final_outputs" / f"{stem}.json",
        )


def _parse_session(
    path: Path,
    thread_id: str,
    turn_id: str,
) -> _TurnTrace:
    """Parse public fields while leaving encrypted reasoning untouched."""
    trace = _TurnTrace()
    active_turn: str | None = None
    matching_thread = False

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return trace
    trace.session_status = "read"

    for line in lines:
        try:
            record = _mapping(json.loads(line))
        except (json.JSONDecodeError, TypeError):
            record = None
        if record is None:
            trace.session_status = "partial"
            continue
        record_type = _text(record.get("type"))
        payload = _mapping(record.get("payload"))
        if payload is None:
            continue

        if record_type == "session_meta":
            matching_thread |= thread_id in {
                _text(payload.get("id")),
                _text(payload.get("session_id")),
            }
            continue
        if record_type == "turn_context":
            active_turn = _text(payload.get("turn_id"))
            continue
        if record_type == "event_msg":
            active_turn = _read_event(payload, turn_id, active_turn, trace)
            continue
        if record_type == "response_item":
            item_turn = _response_turn(payload) or active_turn
            if item_turn == turn_id:
                _read_response_item(payload, trace)

    if not matching_thread:
        return _TurnTrace()
    if trace.encrypted_content_present is None and trace.session_status == "read":
        trace.encrypted_content_present = False
    return trace


def _read_event(
    payload: Mapping[str, object],
    turn_id: str,
    active_turn: str | None,
    trace: _TurnTrace,
) -> str | None:
    """Track the target turn and retain only its final response."""
    event_type = _text(payload.get("type"))
    if event_type == "task_started":
        return _text(payload.get("turn_id"))
    if event_type == "agent_message" and active_turn == turn_id:
        phase = _text(payload.get("phase"))
        if phase in {None, "final_answer"}:
            trace.final_output = _text(payload.get("message")) or trace.final_output
    elif event_type == "task_complete":
        completed_turn = _text(payload.get("turn_id"))
        if completed_turn == turn_id and trace.final_output is None:
            trace.final_output = _text(payload.get("last_agent_message"))
        if completed_turn == active_turn:
            return None
    return active_turn


def _read_response_item(
    payload: Mapping[str, object],
    trace: _TurnTrace,
) -> None:
    """Collect the public reasoning summary or final assistant response."""
    item_type = _text(payload.get("type"))
    if item_type == "reasoning":
        for text in _text_items(payload.get("summary")):
            trace.add_summary(text)
        if _text(payload.get("encrypted_content")):
            trace.encrypted_content_present = True
        return
    if (
        item_type == "message"
        and _text(payload.get("role")) == "assistant"
        and _text(payload.get("phase")) == "final_answer"
    ):
        response = "\n".join(_text_items(payload.get("content"))).strip()
        if response:
            trace.final_output = response


def _merge_turn_items(trace: _TurnTrace, result: TurnResult) -> None:
    """Use completed SDK summaries when the Session has not exposed one."""
    summaries: list[str] = []
    for item in result.items:
        dumped = item.model_dump(mode="json")
        payload = _mapping(dumped.get("root")) or dumped
        if _text(payload.get("type")) != "reasoning":
            continue
        for text in _text_items(payload.get("summary")):
            _append_visible(summaries, text)
    if not trace.summaries:
        trace.summaries.extend(summaries)


def _render_reasoning(
    identity: CodexArtifactIdentity,
    trace: _TurnTrace,
) -> str:
    """Render a readable audit file without claiming hidden chain-of-thought."""
    lines = [
        f"# {identity.company_id} - Day {identity.day}",
        "",
        f"- Invocation: {identity.invocation_id}",
        f"- Thread: {identity.thread_id}",
        f"- Turn: {identity.turn_id}",
        f"- Model: {identity.model}",
        f"- Session: {trace.session_status}",
        f"- Encrypted reasoning present: {_encrypted_status(trace)}",
        "",
        "## Reasoning Summary",
        "",
    ]
    if trace.summaries:
        for index, summary in enumerate(trace.summaries):
            if index:
                lines.extend(("", "---", ""))
            lines.append(summary)
    else:
        lines.append("*No public reasoning summary was available for this turn.*")
    return "\n".join(lines).rstrip() + "\n"


def _response_turn(payload: Mapping[str, object]) -> str | None:
    metadata = _mapping(payload.get("internal_chat_message_metadata_passthrough"))
    if metadata is None:
        return None
    return _text(metadata.get("turn_id")) or _text(metadata.get("turnId"))


def _text_items(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    texts: list[str] = []
    for item in value:
        if isinstance(item, str):
            text = item.strip()
        else:
            mapping = _mapping(item)
            text = _text(mapping.get("text")).strip() if mapping is not None else ""
        if text:
            texts.append(text)
    return tuple(texts)


def _mapping(value: object) -> Mapping[str, object] | None:
    return value if isinstance(value, dict) else None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _append_visible(target: list[str], text: str) -> None:
    normalized = text.strip()
    if normalized:
        target.append(normalized)


def _encrypted_status(trace: _TurnTrace) -> str:
    if trace.encrypted_content_present is True:
        return "yes (not exported)"
    if trace.encrypted_content_present is False:
        return "no"
    return "unknown (Session unavailable or partial)"


def _matches_identity(
    reasoning: str,
    identity: CodexArtifactIdentity,
) -> bool:
    """Reject files exported for another Thread or Turn."""
    return all(
        line in reasoning
        for line in (
            f"- Invocation: {identity.invocation_id}",
            f"- Thread: {identity.thread_id}",
            f"- Turn: {identity.turn_id}",
        )
    )


def _format_json(value: str) -> str:
    try:
        return json.dumps(json.loads(value), ensure_ascii=False, indent=2) + "\n"
    except (json.JSONDecodeError, TypeError):
        return value.rstrip() + "\n"


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
