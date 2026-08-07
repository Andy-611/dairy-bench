"""Deterministic attention plans for sparse Agent decisions."""

from __future__ import annotations

from typing import Self

from pydantic import Field, model_validator

from company_bench.domain.models import Identifier, StrictModel
from company_bench.runtime.models import (
    AgentTurn,
    MarketSide,
    OrderBookView,
    QuoteAlert,
    SimTime,
    Wait,
)

__all__ = [
    "AgentAttention",
    "ArmedWait",
    "AttentionMatch",
    "AttentionRejected",
]


class AttentionRejected(ValueError):
    """A Wait command cannot form a valid attention plan."""


class ArmedWait(StrictModel):
    """One validated, checkpoint-safe attention plan."""

    source_turn_id: Identifier
    armed_at: SimTime
    review_at: SimTime | None = None
    alerts: tuple[QuoteAlert, ...] = Field(max_length=3)

    @model_validator(mode="after")
    def validate_review(self) -> Self:
        """Keep one persisted plan chronological and free of duplicate alerts."""
        if len(self.alerts) != len(set(self.alerts)):
            raise ValueError("armed wait alerts must be unique")
        if self.review_at is None:
            return self
        if self.review_at.day != self.armed_at.day:
            raise ValueError("review_at must be on the day the wait was armed")
        if self.review_at.absolute_minute <= self.armed_at.absolute_minute:
            raise ValueError("review_at must be later than armed_at")
        return self


class AttentionMatch(StrictModel):
    """The deterministic subset of one plan whose thresholds are satisfied."""

    source_turn_id: Identifier
    matched_alerts: tuple[QuoteAlert, ...] = Field(min_length=1, max_length=3)


class AgentAttention:
    """Validate Wait plans and evaluate them against Agent-visible quotes."""

    def arm(self, command: Wait, turn: AgentTurn) -> ArmedWait:
        """Validate and install one immutable attention plan."""
        self._validate_alerts(command.alerts, turn.order_books)
        return ArmedWait(
            source_turn_id=turn.turn_id,
            armed_at=turn.sim_time,
            review_at=self._review_at(command.until, turn),
            alerts=command.alerts,
        )

    def evaluate(
        self,
        plan: ArmedWait,
        order_books: tuple[OrderBookView, ...],
    ) -> AttentionMatch | None:
        """Return every satisfied alert in declaration order, if any."""
        books = {book.product: book for book in order_books}
        matched = tuple(
            alert for alert in plan.alerts if self._matches(alert, books.get(alert.product))
        )
        if not matched:
            return None
        return AttentionMatch(
            source_turn_id=plan.source_turn_id,
            matched_alerts=matched,
        )

    @staticmethod
    def _review_at(until: SimTime | None, turn: AgentTurn) -> SimTime | None:
        runtime = turn.observation.runtime
        now = turn.sim_time
        if until is None:
            review_at = now.plus(runtime.max_wait_minutes)
            return (
                review_at
                if review_at.day == now.day and review_at.minute_of_day < runtime.close_minute
                else None
            )
        if until.absolute_minute <= now.absolute_minute:
            raise AttentionRejected("wait deadline must be later than current time")
        if until.day != now.day:
            raise AttentionRejected("wait deadline must be on the current business day")
        if not runtime.open_minute <= until.minute_of_day < runtime.close_minute:
            raise AttentionRejected("wait deadline must be inside business hours")
        if until.absolute_minute - now.absolute_minute > runtime.max_wait_minutes:
            raise AttentionRejected(
                f"wait deadline cannot exceed {runtime.max_wait_minutes} minutes"
            )
        return until

    def _validate_alerts(
        self,
        alerts: tuple[QuoteAlert, ...],
        order_books: tuple[OrderBookView, ...],
    ) -> None:
        if len(alerts) != len(set(alerts)):
            raise AttentionRejected("wait alerts must be unique")
        books = {book.product: book for book in order_books}
        hidden = next((alert.product for alert in alerts if alert.product not in books), None)
        if hidden is not None:
            raise AttentionRejected(f"alert product {hidden.value} is not visible to this company")
        if any(self._matches(alert, books[alert.product]) for alert in alerts):
            raise AttentionRejected("wait alert must be false when installed")

    @staticmethod
    def _matches(alert: QuoteAlert, book: OrderBookView | None) -> bool:
        if book is None:
            return False
        side = MarketSide.BUY if alert.quote == "best_bid" else MarketSide.SELL
        quote = book.best_price(side)
        if quote is None:
            return False
        return quote >= alert.price if alert.operator == "at_least" else quote <= alert.price
