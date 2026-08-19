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
    SimDay,
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
    armed_on: SimDay
    review_on: SimDay | None = None
    alerts: tuple[QuoteAlert, ...] = Field(max_length=3)

    @model_validator(mode="after")
    def validate_review(self) -> Self:
        """Keep one persisted plan chronological and free of duplicate alerts."""
        if len(self.alerts) != len(set(self.alerts)):
            raise ValueError("armed attention alerts must be unique")
        if self.review_on is None:
            return self
        if self.review_on.week != self.armed_on.week:
            raise ValueError("review_on must remain in the week attention was armed")
        if self.review_on.absolute_day <= self.armed_on.absolute_day:
            raise ValueError("review_on must be later than armed_on")
        if not self.review_on.is_decision_day:
            raise ValueError("review_on must be a company decision day")
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
            armed_on=turn.sim_day,
            review_on=self._review_on(attention.review_after_days, turn),
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
    def _review_on(
        review_after_days: int | None,
        turn: AgentTurn,
    ) -> SimDay | None:
        runtime = turn.observation.runtime
        today = turn.sim_day
        delay = review_after_days or runtime.default_review_days
        if delay > runtime.max_review_days:
            raise AttentionRejected(
                f"attention review delay cannot exceed {runtime.max_review_days} days"
            )
        review_on = today.plus_days(delay)
        if review_on.week != today.week or not review_on.is_decision_day:
            if review_after_days is None:
                return None
            raise AttentionRejected(
                "attention review must remain on Monday-Saturday of the current week"
            )
        return review_on

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
