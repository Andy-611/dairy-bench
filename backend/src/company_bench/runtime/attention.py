"""Deterministic attention plans for sparse Agent decisions."""

from __future__ import annotations

from typing import Self

from pydantic import Field, model_validator

from company_bench.domain.models import Identifier, StrictModel
from company_bench.runtime.models import (
    AgentTurn,
    AttentionPlan,
    MarketSide,
    OrderBookView,
    QuoteAlert,
    SimTime,
)

__all__ = [
    "AgentAttention",
    "ArmedAttention",
    "AttentionMatch",
    "AttentionRejected",
]


class AttentionRejected(ValueError):
    """A submitted attention plan is invalid for the current turn."""


class ArmedAttention(StrictModel):
    """One validated, checkpoint-safe attention plan."""

    source_turn_id: Identifier
    armed_at: SimTime
    review_at: SimTime | None = None
    alerts: tuple[QuoteAlert, ...] = Field(max_length=3)

    @model_validator(mode="after")
    def validate_review(self) -> Self:
        """Keep one persisted plan chronological and free of duplicate alerts."""
        if len(self.alerts) != len(set(self.alerts)):
            raise ValueError("armed attention alerts must be unique")
        if self.review_at is None:
            return self
        if self.review_at.day != self.armed_at.day:
            raise ValueError("review_at must be on the day attention was armed")
        if self.review_at.absolute_minute <= self.armed_at.absolute_minute:
            raise ValueError("review_at must be later than armed_at")
        return self


class AttentionMatch(StrictModel):
    """The deterministic subset of one plan whose thresholds are satisfied."""

    source_turn_id: Identifier
    matched_alerts: tuple[QuoteAlert, ...] = Field(min_length=1, max_length=3)


class AgentAttention:
    """Validate attention plans and evaluate them against Agent-visible quotes."""

    def arm(self, attention: AttentionPlan, turn: AgentTurn) -> ArmedAttention:
        """Validate and install one immutable attention plan."""
        self._validate_alerts(attention.alerts, turn.order_books)
        return ArmedAttention(
            source_turn_id=turn.turn_id,
            armed_at=turn.sim_time,
            review_at=self._review_at(attention.review_after_minutes, turn),
            alerts=attention.alerts,
        )

    def evaluate(
        self,
        plan: ArmedAttention,
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
    def _review_at(
        review_after_minutes: int | None,
        turn: AgentTurn,
    ) -> SimTime | None:
        runtime = turn.observation.runtime
        now = turn.sim_time
        delay = review_after_minutes or runtime.max_review_minutes
        if delay > runtime.max_review_minutes:
            raise AttentionRejected(
                f"attention review delay cannot exceed {runtime.max_review_minutes} minutes"
            )
        review_at = now.plus(delay)
        if review_at.day != now.day or review_at.minute_of_day >= runtime.close_minute:
            if review_after_minutes is None:
                return None
            raise AttentionRejected("attention review must remain inside the business day")
        return review_at

    def _validate_alerts(
        self,
        alerts: tuple[QuoteAlert, ...],
        order_books: tuple[OrderBookView, ...],
    ) -> None:
        if len(alerts) != len(set(alerts)):
            raise AttentionRejected("attention alerts must be unique")
        books = {book.product: book for book in order_books}
        hidden = next((alert.product for alert in alerts if alert.product not in books), None)
        if hidden is not None:
            raise AttentionRejected(f"alert product {hidden.value} is not visible to this company")
        if any(self._matches(alert, books[alert.product]) for alert in alerts):
            raise AttentionRejected("attention alert must be false when installed")

    @staticmethod
    def _matches(alert: QuoteAlert, book: OrderBookView | None) -> bool:
        if book is None:
            return False
        side = MarketSide.BUY if alert.quote == "best_bid" else MarketSide.SELL
        quote = book.best_price(side)
        if quote is None:
            return False
        return quote >= alert.price if alert.operator == "at_least" else quote <= alert.price
