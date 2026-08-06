"""Company actors for the event-driven Dairy Bench runtime."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal
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
    PolicyKind,
    PolicyMetadata,
    ProcessorOperation,
    ProductId,
    Quantity,
    RetailerOperation,
    StrictModel,
)
from company_bench.precision import ECONOMIC_QUANTUM
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
    Produce,
    QuoteLevel,
    SetQuoteLadder,
    SetRetailPrice,
    Transform,
    TurnRecord,
    Wait,
    WakeReason,
)

COMMAND_PROMPT_VERSION: Final = "dairy-company-v3.6"


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


class AgentDecisionConstraints(StrictModel):
    """Explicit decision limits derived from one authoritative turn."""

    market_open_minute: int
    market_close_minute: int
    operation_duration_minutes: int
    delivery_duration_minutes: int
    decision_interval_minutes: int
    max_wait_minutes: int
    used_operation_capacity: Quantity | None
    remaining_operation_capacity: Quantity | None

    @classmethod
    def from_turn(cls, turn: AgentTurn) -> AgentDecisionConstraints:
        """Project runtime and operation limits without owning economic state."""
        runtime = turn.observation.runtime
        operation = turn.observation.daily_operation
        return cls(
            market_open_minute=runtime.open_minute,
            market_close_minute=runtime.close_minute,
            operation_duration_minutes=runtime.operation_duration_minutes,
            delivery_duration_minutes=runtime.delivery_duration_minutes,
            decision_interval_minutes=runtime.decision_interval_minutes,
            max_wait_minutes=runtime.max_wait_minutes,
            used_operation_capacity=None if operation is None else operation.used_capacity,
            remaining_operation_capacity=turn.remaining_operation_capacity,
        )


class AgentCommandInput(StrictModel):
    """Current facts, explicit limits, and one company's private memory."""

    memory: AgentCheckpoint
    turn: AgentTurn
    decision_constraints: AgentDecisionConstraints

    @classmethod
    def from_turn(
        cls,
        memory: AgentCheckpoint,
        turn: AgentTurn,
    ) -> AgentCommandInput:
        """Build the provider projection from one authoritative turn."""
        return cls(
            memory=memory,
            turn=turn,
            decision_constraints=AgentDecisionConstraints.from_turn(turn),
        )


class LlmCompanyAgent:
    """Use one isolated provider gateway and Agent-owned V3 memory."""

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
            input_text=AgentCommandInput.from_turn(
                self._memory.checkpoint(),
                turn,
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
    """Transparent V3 actor for the continuous dairy market."""

    metadata = PolicyMetadata(
        name="event-baseline",
        version="5",
        kind=PolicyKind.BASELINE,
    )

    async def act(self, turn: AgentTurn) -> CompanyCommand:
        """Choose a feasible atomic command from fresh current facts."""
        operation = turn.observation.operation
        if isinstance(operation, FarmOperation):
            return self._farm_command(turn)
        if isinstance(operation, ProcessorOperation):
            return self._processor_command(turn)
        if isinstance(operation, RetailerOperation):
            return self._retailer_command(turn)
        raise TypeError(f"unsupported operation: {type(operation).__name__}")

    @staticmethod
    def _farm_command(turn: AgentTurn) -> CompanyCommand:
        operation = turn.observation.operation
        assert isinstance(operation, FarmOperation)
        if WakeReason.DAY_OPEN in turn.wake_reasons:
            capacity = _remaining_capacity(turn)
            if capacity <= 0:
                return Wait()
            return Produce(
                product=operation.output_product,
                quantity=min(capacity, Decimal("50")),
            )
        command = _sell_ladder(
            turn,
            operation.output_product,
            Decimal("50"),
            (Decimal("1.20"), Decimal("1.40"), Decimal("1.60")),
        )
        return command or Wait()

    @staticmethod
    def _processor_command(turn: AgentTurn) -> CompanyCommand:
        operation = turn.observation.operation
        assert isinstance(operation, ProcessorOperation)
        capacity = _remaining_capacity(turn)
        raw_quantity = turn.observation.quantity(operation.input_product)
        if raw_quantity > 0 and capacity > 0 and turn.active_operation is None:
            return Transform(
                input_product=operation.input_product,
                output_product=operation.output_product,
                input_quantity=min(
                    capacity,
                    raw_quantity,
                    Decimal("50"),
                ),
            )
        sell_command = _sell_ladder(
            turn,
            operation.output_product,
            Decimal("40"),
            (Decimal("2.50"), Decimal("2.65"), Decimal("2.80")),
        )
        if sell_command is not None:
            return sell_command
        order_quantity = _floor_order_quantity(
            min(
                Decimal("50"),
                max(
                    Decimal("0"),
                    capacity - raw_quantity - _pending_quantity(turn, operation.input_product),
                ),
            )
        )
        buy_command = _buy_ladder(
            turn,
            operation.input_product,
            order_quantity,
            (Decimal("1.60"), Decimal("1.50"), Decimal("1.40")),
        )
        return buy_command or Wait()

    @staticmethod
    def _retailer_command(turn: AgentTurn) -> CompanyCommand:
        operation = turn.observation.operation
        assert isinstance(operation, RetailerOperation)
        if turn.observation.retail_price is None:
            return SetRetailPrice(
                product=operation.input_product,
                unit_price=Decimal("3.50"),
            )
        order_quantity = _floor_order_quantity(
            max(
                Decimal("0"),
                Decimal("40")
                - turn.observation.quantity(operation.input_product)
                - _pending_quantity(turn, operation.input_product),
            )
        )
        command = _buy_ladder(
            turn,
            operation.input_product,
            order_quantity,
            (Decimal("2.80"), Decimal("2.65"), Decimal("2.50")),
        )
        return command or Wait()


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
            version="4",
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
        return ("produce", "set_quote_ladder", "wait")
    if isinstance(operation, ProcessorOperation):
        return ("transform", "set_quote_ladder", "wait")
    if isinstance(operation, RetailerOperation):
        return (
            "set_quote_ladder",
            "set_retail_price",
            "wait",
        )
    raise TypeError(f"unsupported operation: {type(operation).__name__}")


def _command_instructions(allowed: tuple[CommandName, ...]) -> str:
    """Build the stable provider-neutral V3 command prompt."""
    commands = ", ".join(allowed)
    return (
        "You are the sole Agent for one dairy company in a continuous spot market. "
        "Your sole objective is to maximize your own company's profit. "
        "Orders lock real cash or FEFO inventory, crossing quotes trade immediately at "
        "the resting price. Purchases arrive after decision_constraints."
        "delivery_duration_minutes; production and transformation finish after "
        "decision_constraints.operation_duration_minutes while market commands remain "
        "available. Order books list every anonymous price level with aggregate quantity "
        "and order_count. queue_ahead_quantity is the same-price quantity ahead of your "
        "order. "
        "inventory_expiry splits owned spot inventory into available and reserved "
        "quantities by expiry; in-transit lots remain in pending_deliveries. "
        "marked_surplus is guaranteed marked asset value minus initial cash, including "
        "reserved assets, pending deliveries, and active-operation output at reference "
        "values. For productive companies, observation.daily_operation supplies "
        "K=daily_capacity and c=daily_base_unit_cost; decision_constraints supplies "
        "u=used_operation_capacity and remaining_operation_capacity, while "
        "observation.operation.cost.curvature supplies curvature; "
        "for a new quantity q, cash cost is C(u+q)-C(u), where "
        "C(x)=c*x+curvature*c*x^2/(2*K). Use only supplied facts and "
        "submit exactly one atomic command; never invent identity, time, or state version. "
        "Every production, transformation, and quote-level quantity must be at least "
        f"{ECONOMIC_QUANTUM} and use at most four decimal places (an exact multiple of "
        f"{ECONOMIC_QUANTUM}); never submit a dust quantity. set_quote_ladder declares "
        "the complete target state for one product and side: use zero to three unique "
        "levels ordered best-to-worst (buy prices descending, sell prices ascending), "
        "and use [] to cancel that ladder. The complete update is atomic. Exact unchanged "
        "price-quantity levels keep their order identity and priority; every changed level "
        "loses its old priority. Total asks require real inventory and total bids require "
        "real cash collateral. Use wait when no action is justified: set until to null to "
        "use the runtime's bounded fallback review when it remains before market close, or "
        "select an earlier deadline within decision_constraints.max_wait_minutes and "
        "before decision_constraints.market_close_minute. Set alerts to [] when no price "
        "condition is needed; otherwise provide up to three OR price alerts over the "
        "best visible quote: bids[0].unit_price for best_bid or asks[0].unit_price for "
        "best_ask. Every alert must still be false when armed. "
        "Background monitoring consumes no turn, but every model call counts against the "
        "daily turn budget supplied in the turn. Do not poll for ordinary quote changes. "
        f"Authorized commands: {commands}."
    )


def _floor_order_quantity(value: Decimal) -> Decimal:
    """Floor a feasible baseline order to the market quantum."""
    return value.quantize(ECONOMIC_QUANTUM, rounding=ROUND_DOWN)


def _sell_ladder(
    turn: AgentTurn,
    product: ProductId,
    limit: Decimal,
    prices: tuple[Decimal, ...],
) -> SetQuoteLadder | None:
    """Quote available plus already-reserved stock without ladder oscillation."""
    reserved = sum(
        (
            order.remaining_quantity
            for order in turn.open_orders
            if order.side is MarketSide.SELL and order.product is product
        ),
        start=Decimal(),
    )
    quantity = min(limit, turn.observation.quantity(product) + reserved)
    return _target_ladder(turn, MarketSide.SELL, product, quantity, prices)


def _buy_ladder(
    turn: AgentTurn,
    product: ProductId,
    quantity: Decimal,
    prices: tuple[Decimal, ...],
) -> SetQuoteLadder | None:
    """Quote a stock gap within conservative full-collateral buying power."""
    buying_power = turn.available_cash + turn.reserved_cash
    affordable = _floor_order_quantity(buying_power / prices[0])
    return _target_ladder(
        turn,
        MarketSide.BUY,
        product,
        min(quantity, affordable),
        prices,
    )


def _target_ladder(
    turn: AgentTurn,
    side: MarketSide,
    product: ProductId,
    quantity: Decimal,
    prices: tuple[Decimal, ...],
) -> SetQuoteLadder | None:
    """Return only a materially different target ladder."""
    levels = _split_ladder(quantity, prices)
    existing = tuple(
        sorted(
            (
                order
                for order in turn.open_orders
                if order.side is side and order.product is product
            ),
            key=lambda order: order.limit_price,
            reverse=side is MarketSide.BUY,
        )
    )
    if len(existing) == len(levels) and all(
        order.remaining_quantity == level.quantity and order.limit_price == level.limit_price
        for order, level in zip(existing, levels, strict=True)
    ):
        return None
    return SetQuoteLadder(product=product, side=side, levels=levels)


def _split_ladder(
    quantity: Decimal,
    prices: tuple[Decimal, ...],
) -> tuple[QuoteLevel, ...]:
    """Split one feasible total across up to three 40/40/20 target levels."""
    total = _floor_order_quantity(quantity)
    if total <= 0:
        return ()
    count = min(len(prices), int(total / ECONOMIC_QUANTUM))
    if count == 1:
        quantities = (total,)
    elif count == 2:
        first = _floor_order_quantity(total / 2)
        quantities = (first, total - first)
    else:
        first = _floor_order_quantity(total * Decimal("0.4"))
        second = _floor_order_quantity(total * Decimal("0.4"))
        quantities = (first, second, total - first - second)
    return tuple(
        QuoteLevel(quantity=level_quantity, limit_price=price)
        for price, level_quantity in zip(prices, quantities, strict=False)
    )


def _pending_quantity(turn: AgentTurn, product: ProductId) -> Decimal:
    return sum(
        (delivery.quantity for delivery in turn.pending_deliveries if delivery.product is product),
        start=Decimal("0"),
    )


def _remaining_capacity(turn: AgentTurn) -> Decimal:
    """Return a productive company's private realized remaining capacity."""
    remaining = turn.remaining_operation_capacity
    if remaining is None:
        raise TypeError("productive baseline turn requires daily operation state")
    return remaining


def _estimated_tokens(*parts: str) -> int:
    """Bound provider input with the same deterministic four-character estimate."""
    characters = sum(len(part) for part in parts)
    return max(1, (characters + 3) // 4)
