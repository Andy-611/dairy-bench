"""Company-private, checkpointable conversation memory."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Final, Literal, Self

from pydantic import Field, model_validator

from company_bench.domain.models import CompanyId, Identifier, StrictModel
from company_bench.runtime.models import TurnRecord

_CHECKPOINT_SCHEMA_VERSION: Final = 5
_DEFAULT_MAX_TOKENS: Final = 16_384
_DEFAULT_CHARS_PER_TOKEN: Final = 4
_SUMMARY_OMISSION: Final = "[older memory omitted]"


class MemorySummary(StrictModel):
    """Deterministic digest of exchanges removed from the active context."""

    run_id: Identifier
    company_id: CompanyId
    through_turn_id: Identifier
    through_absolute_minute: int = Field(ge=0)
    through_apply_sequence: int = Field(ge=1)
    exchange_count: int = Field(ge=1)
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    content: str = Field(min_length=1)


class MemoryExchange(TurnRecord):
    """One complete private AgentTurn to command to outcome cycle."""

    @classmethod
    def from_record(cls, record: TurnRecord) -> Self:
        """Convert an authoritative journal record into Agent memory."""
        return cls.model_validate(record.model_dump())


type MemorySummarizer = Callable[
    [MemorySummary | None, tuple[MemoryExchange, ...]],
    str,
]


class AgentCheckpoint(StrictModel):
    """Portable state required to restore one company's memory exactly."""

    schema_version: Literal[5] = _CHECKPOINT_SCHEMA_VERSION
    run_id: Identifier
    company_id: CompanyId
    revision: int = Field(ge=0)
    max_tokens: int = Field(ge=1)
    chars_per_token: int = Field(ge=1)
    summary: MemorySummary | None = None
    exchanges: tuple[MemoryExchange, ...] = ()

    @model_validator(mode="after")
    def validate_history(self) -> Self:
        """Require one ordered, company-private, internally complete history."""
        compacted = 0
        previous_minute = -1
        previous_apply_sequence = 0
        if self.summary is not None:
            if self.summary.run_id != self.run_id:
                raise ValueError("summary run_id must match the checkpoint")
            if self.summary.company_id != self.company_id:
                raise ValueError("summary company_id must match the checkpoint")
            compacted = self.summary.exchange_count
            previous_minute = self.summary.through_absolute_minute
            previous_apply_sequence = self.summary.through_apply_sequence

        turn_ids: set[str] = set()
        command_ids: set[str] = set()
        for exchange in self.exchanges:
            if exchange.run_id != self.run_id:
                raise ValueError("exchange run_id must match the checkpoint")
            if exchange.turn.company_id != self.company_id:
                raise ValueError("exchange company_id must match the checkpoint")
            if exchange.turn.turn_id in turn_ids:
                raise ValueError("turn_id must be unique in active memory")
            if exchange.envelope.command_id in command_ids:
                raise ValueError("command_id must be unique in active memory")
            if exchange.turn.sim_time.absolute_minute < previous_minute:
                raise ValueError("memory exchanges must be chronological")
            if exchange.outcome.apply_sequence <= previous_apply_sequence:
                raise ValueError("memory apply_sequence must increase")
            turn_ids.add(exchange.turn.turn_id)
            command_ids.add(exchange.envelope.command_id)
            previous_minute = exchange.turn.sim_time.absolute_minute
            previous_apply_sequence = exchange.outcome.apply_sequence

        if self.summary is not None and self.summary.through_turn_id in turn_ids:
            raise ValueError("a summarized turn cannot remain in active memory")
        if self.revision != compacted + len(self.exchanges):
            raise ValueError("revision must equal summarized and active exchanges")
        return self


class ConversationMemory:
    """Own one Agent's bounded context and durable checkpoint."""

    def __init__(
        self,
        run_id: Identifier,
        company_id: CompanyId,
        *,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
        chars_per_token: int = _DEFAULT_CHARS_PER_TOKEN,
        summarizer: MemorySummarizer | None = None,
    ) -> None:
        if max_tokens <= 0:
            raise ValueError("max_tokens must be positive")
        if chars_per_token <= 0:
            raise ValueError("chars_per_token must be positive")
        self._run_id = run_id
        self._company_id = company_id
        self._max_tokens = max_tokens
        self._chars_per_token = chars_per_token
        self._summarizer = summarizer or deterministic_summary
        self._summary: MemorySummary | None = None
        self._exchanges: tuple[MemoryExchange, ...] = ()

    @classmethod
    def restore(
        cls,
        run_id: Identifier,
        company_id: CompanyId,
        checkpoint: AgentCheckpoint,
        *,
        summarizer: MemorySummarizer | None = None,
    ) -> Self:
        """Restore state while refusing cross-run or cross-company reuse."""
        if checkpoint.run_id != run_id:
            raise ValueError("checkpoint belongs to another run")
        if checkpoint.company_id != company_id:
            raise ValueError("checkpoint belongs to another company")
        memory = cls(
            run_id,
            company_id,
            max_tokens=checkpoint.max_tokens,
            chars_per_token=checkpoint.chars_per_token,
            summarizer=summarizer,
        )
        memory._summary = checkpoint.summary
        memory._exchanges = checkpoint.exchanges
        return memory

    @property
    def summary(self) -> MemorySummary | None:
        """Return the immutable long-horizon summary."""
        return self._summary

    @property
    def exchanges(self) -> tuple[MemoryExchange, ...]:
        """Return recent complete exchanges in chronological order."""
        return self._exchanges

    @property
    def max_tokens(self) -> int:
        """Return the configured provider-neutral context budget."""
        return self._max_tokens

    @property
    def estimated_tokens(self) -> int:
        """Estimate current context tokens from its serialized character count."""
        return self._estimate(self._summary, self._exchanges)

    def remember(self, exchange: MemoryExchange) -> None:
        """Atomically add one complete exchange and compact only whole cycles."""
        candidate = self._build_checkpoint(
            self._summary,
            (*self._exchanges, exchange),
        )
        summary, exchanges = self._fit_budget(
            candidate.summary,
            candidate.exchanges,
        )
        final = self._build_checkpoint(summary, exchanges)
        self._summary = final.summary
        self._exchanges = final.exchanges

    def context_json(self) -> str:
        """Render only Agent-visible memory state for a provider request."""
        return self.checkpoint().model_dump_json(
            include={"summary", "exchanges"},
            exclude_defaults=True,
            exclude_none=True,
        )

    def checkpoint(self) -> AgentCheckpoint:
        """Return an immutable, JSON-serializable recovery value."""
        return self._build_checkpoint(self._summary, self._exchanges)

    def _fit_budget(
        self,
        summary: MemorySummary | None,
        exchanges: tuple[MemoryExchange, ...],
    ) -> tuple[MemorySummary | None, tuple[MemoryExchange, ...]]:
        """Compact the oldest cycles while always retaining the newest one."""
        while len(exchanges) > 1 and self._estimate(summary, exchanges) > self._max_tokens:
            summary = self._summarize(summary, exchanges[:1])
            exchanges = exchanges[1:]
        if summary is not None and self._estimate(summary, exchanges) > self._max_tokens:
            summary = self._trim_summary(summary, exchanges)
        return summary, exchanges

    def _summarize(
        self,
        previous: MemorySummary | None,
        removed: tuple[MemoryExchange, ...],
    ) -> MemorySummary:
        """Build a validated summary from an injected pure function."""
        content = self._summarizer(previous, removed).strip()
        if not content:
            raise ValueError("memory summarizer returned empty content")
        latest = removed[-1]
        return MemorySummary(
            run_id=self._run_id,
            company_id=self._company_id,
            through_turn_id=latest.turn.turn_id,
            through_absolute_minute=latest.turn.sim_time.absolute_minute,
            through_apply_sequence=latest.outcome.apply_sequence,
            exchange_count=(previous.exchange_count if previous else 0) + len(removed),
            source_hash=_source_hash(previous, removed),
            content=content,
        )

    def _trim_summary(
        self,
        summary: MemorySummary,
        exchanges: tuple[MemoryExchange, ...],
    ) -> MemorySummary:
        """Fit summary text when possible without dropping the latest cycle."""
        minimal = summary.model_copy(update={"content": _SUMMARY_OMISSION})
        if self._estimate(minimal, exchanges) > self._max_tokens:
            return minimal

        low = 1
        high = len(summary.content)
        best = minimal
        while low <= high:
            middle = (low + high) // 2
            candidate = summary.model_copy(update={"content": _tail(summary.content, middle)})
            if self._estimate(candidate, exchanges) <= self._max_tokens:
                best = candidate
                low = middle + 1
            else:
                high = middle - 1
        return best

    def _estimate(
        self,
        summary: MemorySummary | None,
        exchanges: tuple[MemoryExchange, ...],
    ) -> int:
        """Estimate tokens using the configured characters-per-token ratio."""
        checkpoint = self._build_checkpoint(summary, exchanges)
        characters = len(
            checkpoint.model_dump_json(
                include={"summary", "exchanges"},
                exclude_defaults=True,
                exclude_none=True,
            )
        )
        return max(1, (characters + self._chars_per_token - 1) // self._chars_per_token)

    def _build_checkpoint(
        self,
        summary: MemorySummary | None,
        exchanges: tuple[MemoryExchange, ...],
    ) -> AgentCheckpoint:
        """Build and validate the current immutable recovery value."""
        revision = (summary.exchange_count if summary else 0) + len(exchanges)
        return AgentCheckpoint(
            run_id=self._run_id,
            company_id=self._company_id,
            revision=revision,
            max_tokens=self._max_tokens,
            chars_per_token=self._chars_per_token,
            summary=summary,
            exchanges=exchanges,
        )


def deterministic_summary(
    previous: MemorySummary | None,
    exchanges: tuple[MemoryExchange, ...],
) -> str:
    """Summarize exact command outcomes without an LLM or hidden state."""
    lines = [previous.content] if previous is not None else []
    lines.extend(_exchange_line(exchange) for exchange in exchanges)
    return "\n".join(lines)


def _exchange_line(exchange: MemoryExchange) -> str:
    """Render one compact, deterministic, provider-neutral memory line."""
    outcome = exchange.outcome
    ladder = outcome.quote_ladder_result
    order_ids = ",".join(level.order_id for level in ladder.levels) if ladder else ""
    detail = outcome.reason or order_ids or "-"
    detail = " ".join(detail.split())
    return (
        f"{exchange.turn.turn_id}@{exchange.turn.sim_time.absolute_minute}:"
        f"{exchange.envelope.command.model_dump_json()}"
        f"=>{outcome.status.value}[state={outcome.resulting_state_version},"
        f"apply={outcome.apply_sequence},detail={detail}]"
    )


def _source_hash(
    previous: MemorySummary | None,
    exchanges: tuple[MemoryExchange, ...],
) -> str:
    """Hash the exact summary chain and removed cycles."""
    digest = hashlib.sha256()
    if previous is not None:
        digest.update(previous.model_dump_json().encode())
    for exchange in exchanges:
        digest.update(exchange.model_dump_json().encode())
    return digest.hexdigest()


def _tail(content: str, limit: int) -> str:
    """Keep the newest complete summary lines without returning fragments."""
    if len(content) <= limit:
        return content
    if limit <= len(_SUMMARY_OMISSION):
        return _SUMMARY_OMISSION

    retained: list[str] = []
    length = 0
    for line in reversed(content.splitlines()):
        candidate_length = length + len(line) + (1 if retained else 0)
        if candidate_length > limit:
            break
        retained.append(line)
        length = candidate_length
    if not retained:
        return _SUMMARY_OMISSION

    suffix = "\n".join(reversed(retained))
    marked = f"{_SUMMARY_OMISSION}\n{suffix}"
    return marked if len(marked) <= limit else suffix
