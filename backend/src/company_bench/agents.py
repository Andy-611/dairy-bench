"""Company actors for the event-driven Dairy Bench runtime."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Final, Protocol, runtime_checkable
from uuid import uuid4

from company_bench.agent_models import (
    CommandGateway,
    CommandModelRequest,
    CommandName,
    ModelInfrastructureError,
    ModelOutputError,
    PolicyInfrastructureError,
)
from company_bench.diagnostics import bounded_error
from company_bench.memory import AgentCheckpoint, ConversationMemory, MemoryExchange
from company_bench.models import (
    CompanyId,
    FarmOperation,
    MilkProcessedEvent,
    PolicyKind,
    PolicyMetadata,
    ProcessorOperation,
    ProductId,
    RetailerOperation,
    StrictModel,
)
from company_bench.run_models import (
    InvocationOutcome,
    PolicyAuditSink,
    PolicyInvocation,
    TokenUsage,
)
from company_bench.runtime_models import (
    AgentTurn,
    CompanyCommand,
    MarketSide,
    PlaceOrder,
    Produce,
    SetRetailPrice,
    Transform,
    TurnRecord,
    Wait,
    WakeReason,
)

COMMAND_PROMPT_VERSION: Final = "dairy-company-v2"


class CompanyAgent(Protocol):
    """Return exactly one atomic command for a runtime-owned turn."""

    metadata: PolicyMetadata

    async def act(self, turn: AgentTurn) -> CompanyCommand:
        """Choose one command without changing economic state."""
        ...


class ReplayedProtocolError(RuntimeError):
    """Reproduce one journaled Agent protocol failure without a provider call."""


class ReplayDriftError(RuntimeError):
    """Stop a replay whose source stream no longer matches runtime facts."""


@runtime_checkable
class TurnMemory(Protocol):
    """Optional Agent-owned sink for complete command/outcome cycles."""

    def remember(self, record: TurnRecord) -> None:
        """Retain one completed turn for the next model call."""
        ...


@runtime_checkable
class CheckpointMemory(Protocol):
    """Optional Agent-owned recovery state."""

    def checkpoint(self) -> AgentCheckpoint:
        """Return an immutable Agent checkpoint."""
        ...


@runtime_checkable
class EpisodeCompletionGuard(Protocol):
    """Optional Agent validation performed before an episode can be scored."""

    def ensure_episode_complete(self) -> None:
        """Reject an incomplete or overlong Agent-owned source stream."""
        ...


class AgentCommandInput(StrictModel):
    """Current facts plus one company's bounded private conversation."""

    memory: AgentCheckpoint
    turn: AgentTurn


class LlmCompanyAgent:
    """Use one isolated provider gateway and Agent-owned V2 memory."""

    metadata: PolicyMetadata

    def __init__(
        self,
        *,
        run_id: str,
        company_id: CompanyId,
        gateway: CommandGateway,
        audit_sink: PolicyAuditSink,
        metadata: PolicyMetadata,
        checkpoint: AgentCheckpoint | None = None,
        memory_token_budget: int = 12_288,
        max_prompt_tokens: int = 16_384,
    ) -> None:
        if memory_token_budget >= max_prompt_tokens:
            raise ValueError("memory budget must leave room for fresh turn facts")
        self.metadata = metadata
        self._run_id = run_id
        self._company_id = company_id
        self._gateway = gateway
        self._audit_sink = audit_sink
        self._max_prompt_tokens = max_prompt_tokens
        self._memory = (
            ConversationMemory.restore(run_id, company_id, checkpoint)
            if checkpoint is not None
            else ConversationMemory(
                run_id,
                company_id,
                max_tokens=memory_token_budget,
            )
        )
        self._pending_invocations: dict[str, PolicyInvocation] = {}

    async def act(self, turn: AgentTurn) -> CompanyCommand:
        """Generate and audit exactly one role-authorized command."""
        if turn.company_id != self._company_id:
            raise ValueError("an Agent cannot control another company")
        allowed_commands = _allowed_commands(turn)
        instructions = _command_instructions(allowed_commands)
        request = CommandModelRequest(
            invocation_id=f"inv_{uuid4().hex}",
            run_id=self._run_id,
            turn=turn,
            instructions=instructions,
            input_text=AgentCommandInput(
                memory=self._memory.checkpoint(),
                turn=turn,
            ).model_dump_json(
                exclude_defaults=True,
                exclude_none=True,
            ),
            allowed_commands=allowed_commands,
        )
        started_at = datetime.now(UTC)
        try:
            if (
                _estimated_tokens(request.instructions, request.input_text)
                > self._max_prompt_tokens
            ):
                raise ModelOutputError("Agent context exceeds the configured prompt-token limit")
            result = await self._gateway.generate_command(request)
        except ModelOutputError as error:
            self._record_failure(
                request,
                started_at,
                InvocationOutcome.AGENT_ERROR,
                error,
            )
            raise
        except ModelInfrastructureError as error:
            self._record_failure(
                request,
                started_at,
                InvocationOutcome.INFRASTRUCTURE_ERROR,
                error,
            )
            raise PolicyInfrastructureError(str(error)) from error
        except Exception as error:
            self._record_failure(
                request,
                started_at,
                InvocationOutcome.INFRASTRUCTURE_ERROR,
                error,
            )
            raise PolicyInfrastructureError(
                f"unexpected model gateway failure: {type(error).__name__}"
            ) from error

        invocation = PolicyInvocation(
            **self._invocation_fields(request, started_at),
            finished_at=datetime.now(UTC),
            outcome=InvocationOutcome.SUCCESS,
            command=result.command,
            response_id=result.response_id,
            provider_turn_id=result.response_id,
            request_id=result.request_id,
            usage=result.usage,
            attempts=result.attempts,
            latency_ms=result.latency_ms,
        )
        self._pending_invocations[turn.turn_id] = invocation
        self._audit_sink.record_invocation(invocation)
        return result.command

    def remember(self, record: TurnRecord) -> None:
        """Store the complete cycle and attach its outcome to provider audit."""
        if record.run_id != self._run_id or record.turn.company_id != self._company_id:
            raise ValueError("turn record belongs to another Agent")
        self._memory.remember(MemoryExchange.from_record(record))
        invocation = self._pending_invocations.pop(record.turn.turn_id, None)
        if invocation is not None:
            self._audit_sink.record_invocation(
                invocation.model_copy(
                    update={
                        "apply_sequence": record.outcome.apply_sequence,
                        "command_outcome": record.outcome,
                    }
                )
            )

    def checkpoint(self) -> AgentCheckpoint:
        """Return this Agent's portable memory state."""
        return self._memory.checkpoint()

    def _record_failure(
        self,
        request: CommandModelRequest,
        started_at: datetime,
        outcome: InvocationOutcome,
        error: Exception,
    ) -> None:
        model_error = error if isinstance(error, ModelOutputError) else None
        self._audit_sink.record_invocation(
            PolicyInvocation(
                **self._invocation_fields(request, started_at),
                finished_at=datetime.now(UTC),
                outcome=outcome,
                error_kind=type(error).__name__,
                error_message=bounded_error(error),
                response_id=model_error.response_id if model_error else None,
                provider_turn_id=model_error.response_id if model_error else None,
                request_id=model_error.request_id if model_error else None,
                usage=model_error.usage if model_error else TokenUsage(),
                attempts=model_error.attempts if model_error else 1,
                latency_ms=model_error.latency_ms if model_error else 0,
            )
        )

    def _invocation_fields(
        self,
        request: CommandModelRequest,
        started_at: datetime,
    ) -> dict[str, object]:
        turn = request.turn
        return {
            "invocation_id": request.invocation_id,
            "run_id": request.run_id,
            "company_id": turn.company_id,
            "day": turn.observation.day,
            "observation": turn.observation,
            "provider": self.metadata.provider or "unknown",
            "model": self.metadata.model or "unknown",
            "prompt_version": self.metadata.prompt_version or COMMAND_PROMPT_VERSION,
            "prompt_hash": hashlib.sha256(
                f"{request.instructions}\0{request.input_text}".encode()
            ).hexdigest(),
            "started_at": started_at,
            "domain_turn_id": turn.turn_id,
            "sim_minute": turn.sim_time.absolute_minute,
            "state_version": turn.state_version,
        }


class FixedCommandAgent:
    """Return commands from a deterministic iterable for exact tests."""

    metadata = PolicyMetadata(
        name="fixed-command",
        kind=PolicyKind.BASELINE,
    )

    def __init__(self, commands: Iterable[CompanyCommand]) -> None:
        self._commands = iter(commands)

    async def act(self, turn: AgentTurn) -> CompanyCommand:
        """Return the next configured command."""
        try:
            return next(self._commands)
        except StopIteration as error:
            raise ValueError("fixed Agent command stream is exhausted") from error


class BaselineCompanyAgent:
    """Transparent V2 actor that follows the scenario's market phases."""

    metadata = PolicyMetadata(
        name="event-baseline",
        version="2",
        kind=PolicyKind.BASELINE,
    )

    async def act(self, turn: AgentTurn) -> CompanyCommand:
        """Choose a feasible atomic command from fresh current facts."""
        operation = turn.observation.operation
        minute = turn.sim_time.minute_of_day
        if isinstance(operation, FarmOperation):
            return self._farm_command(turn, minute)
        if isinstance(operation, ProcessorOperation):
            return self._processor_command(turn, minute)
        if isinstance(operation, RetailerOperation):
            return self._retailer_command(turn, minute)
        raise TypeError(f"unsupported operation: {type(operation).__name__}")

    @staticmethod
    def _farm_command(turn: AgentTurn, minute: int) -> CompanyCommand:
        operation = turn.observation.operation
        assert isinstance(operation, FarmOperation)
        if minute >= turn.observation.runtime.raw_market_clear_minute:
            return Wait()
        if WakeReason.DAY_OPEN in turn.wake_reasons:
            return Produce(
                product=operation.output_product,
                quantity=min(operation.daily_capacity, Decimal("50")),
            )
        if _has_order(turn, MarketSide.SELL, operation.output_product):
            return Wait()
        quantity = min(turn.observation.quantity(operation.output_product), Decimal("50"))
        return (
            PlaceOrder(
                side=MarketSide.SELL,
                product=operation.output_product,
                quantity=quantity,
                limit_price=Decimal("1.40"),
            )
            if quantity > 0
            else Wait()
        )

    @staticmethod
    def _processor_command(turn: AgentTurn, minute: int) -> CompanyCommand:
        operation = turn.observation.operation
        assert isinstance(operation, ProcessorOperation)
        runtime = turn.observation.runtime
        if minute < runtime.raw_market_clear_minute:
            return (
                Wait()
                if _has_order(turn, MarketSide.BUY, operation.input_product)
                else PlaceOrder(
                    side=MarketSide.BUY,
                    product=operation.input_product,
                    quantity=min(operation.daily_input_capacity, Decimal("50")),
                    limit_price=Decimal("1.60"),
                )
            )
        if minute >= runtime.bottled_market_clear_minute:
            return Wait()
        if _has_order(turn, MarketSide.SELL, operation.output_product):
            return Wait()
        raw_quantity = turn.observation.quantity(operation.input_product)
        transformed_now = (
            any(isinstance(event, MilkProcessedEvent) for event in turn.previous_outcome.events)
            if turn.previous_outcome is not None
            else False
        )
        if raw_quantity > 0 and not transformed_now:
            return Transform(
                input_product=operation.input_product,
                output_product=operation.output_product,
                input_quantity=min(
                    operation.daily_input_capacity,
                    raw_quantity,
                    Decimal("50"),
                ),
            )
        bottled = min(
            turn.observation.quantity(operation.output_product),
            Decimal("40"),
        )
        return (
            PlaceOrder(
                side=MarketSide.SELL,
                product=operation.output_product,
                quantity=bottled,
                limit_price=Decimal("2.50"),
            )
            if bottled > 0
            else Wait()
        )

    @staticmethod
    def _retailer_command(turn: AgentTurn, minute: int) -> CompanyCommand:
        operation = turn.observation.operation
        assert isinstance(operation, RetailerOperation)
        if minute < turn.observation.runtime.bottled_market_clear_minute:
            return (
                Wait()
                if _has_order(turn, MarketSide.BUY, operation.input_product)
                else PlaceOrder(
                    side=MarketSide.BUY,
                    product=operation.input_product,
                    quantity=Decimal("40"),
                    limit_price=Decimal("2.80"),
                )
            )
        if turn.observation.retail_price is not None:
            return Wait()
        return SetRetailPrice(
            product=operation.input_product,
            unit_price=Decimal("3.50"),
        )


class ReplayCompanyAgent:
    """Replay one company's command stream with observation-drift checks."""

    metadata: PolicyMetadata

    def __init__(
        self,
        company_id: str,
        records: tuple[TurnRecord, ...],
        *,
        completed_turns: int = 0,
    ) -> None:
        source_run_ids = {record.run_id for record in records}
        if len(source_run_ids) != 1:
            raise ValueError("replay requires one nonempty source run journal")
        self.metadata = PolicyMetadata(
            name="turn-replay",
            version="2",
            kind=PolicyKind.REPLAY,
            source_run_id=source_run_ids.pop(),
        )
        self._company_id = company_id
        self._records = tuple(
            sorted(
                (record for record in records if record.turn.company_id == company_id),
                key=lambda record: record.outcome.apply_sequence,
            )
        )
        if not 0 <= completed_turns <= len(self._records):
            raise ValueError("completed_turns exceeds the source command stream")
        self._index = completed_turns

    async def act(self, turn: AgentTurn) -> CompanyCommand:
        """Return the next source command only when current facts match."""
        if turn.company_id != self._company_id:
            raise ReplayDriftError("a replay Agent cannot control another company")
        if self._index >= len(self._records):
            raise ReplayDriftError(f"source run has no remaining turn for {self._company_id}")
        source = self._records[self._index]
        actual_hash = observation_hash(turn)
        if source.observation_hash != actual_hash:
            raise ReplayDriftError(
                f"replay observation drift at {self._company_id} turn {self._index + 1}"
            )
        self._index += 1
        if source.protocol_error is not None:
            raise ReplayedProtocolError(source.protocol_error)
        return source.envelope.command

    def remember(self, record: TurnRecord) -> None:
        """Verify that replay reproduced the source command outcome exactly."""
        if record.turn.company_id != self._company_id or self._index == 0:
            raise ReplayDriftError("replay outcome arrived without a matching source turn")
        source = self._records[self._index - 1]
        same_outcome = source.outcome.model_dump_json(
            exclude={"turn_id", "command_id"}
        ) == record.outcome.model_dump_json(exclude={"turn_id", "command_id"})
        if (
            record.envelope.command != source.envelope.command
            or record.protocol_error != source.protocol_error
            or not same_outcome
        ):
            raise ReplayDriftError(f"replay outcome drift at {self._company_id} turn {self._index}")

    def ensure_episode_complete(self) -> None:
        """Require replay to consume the complete company source stream."""
        remaining = len(self._records) - self._index
        if remaining:
            raise ReplayDriftError(
                f"source run has {remaining} unconsumed turn(s) for {self._company_id}"
            )


def observation_hash(turn: AgentTurn) -> str:
    """Hash every model-visible fact while excluding runtime-owned identities."""
    payload = turn.model_dump_json(
        exclude={
            "turn_id": True,
            "wake_signals": True,
            "previous_outcome": {
                "turn_id",
                "command_id",
                "company_id",
                "apply_sequence",
            },
        }
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _allowed_commands(turn: AgentTurn) -> tuple[CommandName, ...]:
    """Expose only commands authorized for the observed company role."""
    operation = turn.observation.operation
    if isinstance(operation, FarmOperation):
        return ("produce", "place_order", "cancel_order", "wait")
    if isinstance(operation, ProcessorOperation):
        return ("transform", "place_order", "cancel_order", "wait")
    if isinstance(operation, RetailerOperation):
        return ("place_order", "cancel_order", "set_retail_price", "wait")
    raise TypeError(f"unsupported operation: {type(operation).__name__}")


def _command_instructions(allowed: tuple[CommandName, ...]) -> str:
    """Build the stable provider-neutral V2 command prompt."""
    commands = ", ".join(allowed)
    return (
        "You are the sole Agent for one dairy company in an event-driven benchmark. "
        "Use only the supplied current observation, visible events, active orders, "
        "previous outcome, and your own private memory. Submit exactly one atomic "
        "command; never submit a daily plan or invent identity, time, or state version. "
        "The economic engine alone decides feasibility and effects. Use wait when no "
        f"action is justified. Authorized commands: {commands}."
    )


def _has_order(
    turn: AgentTurn,
    side: MarketSide,
    product: ProductId,
) -> bool:
    return any(order.side is side and order.product is product for order in turn.open_orders)


def _estimated_tokens(*parts: str) -> int:
    """Bound provider input with the same deterministic four-character estimate."""
    characters = sum(len(part) for part in parts)
    return max(1, (characters + 3) // 4)
