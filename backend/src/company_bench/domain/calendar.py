"""Canonical 52-week simulation calendar shared by every runtime layer."""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "DAYS_PER_WEEK",
    "DECISION_DAYS_PER_WEEK",
    "SimDay",
    "TradingCalendar",
    "Weekday",
]

DAYS_PER_WEEK = 7
DECISION_DAYS_PER_WEEK = 6


class Weekday(StrEnum):
    """Named day within one dairy trading week."""

    MONDAY = "monday"
    TUESDAY = "tuesday"
    WEDNESDAY = "wednesday"
    THURSDAY = "thursday"
    FRIDAY = "friday"
    SATURDAY = "saturday"
    SUNDAY = "sunday"

    @property
    def number(self) -> int:
        """Return the ISO-style one-based day number."""
        return tuple(type(self)).index(self) + 1

    @property
    def is_decision_day(self) -> bool:
        """Return whether company Agents may act on this weekday."""
        return self is not Weekday.SUNDAY


class SimDay(BaseModel):
    """One immutable day on the episode-wide simulation clock."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    absolute_day: int = Field(ge=1)

    @classmethod
    def at(cls, *, week: int, weekday: Weekday = Weekday.MONDAY) -> Self:
        """Build a simulation day from a one-based week and named weekday."""
        if week < 1:
            raise ValueError("week must be positive")
        return cls(absolute_day=(week - 1) * DAYS_PER_WEEK + weekday.number)

    @property
    def week(self) -> int:
        """Return the one-based trading week."""
        return (self.absolute_day - 1) // DAYS_PER_WEEK + 1

    @property
    def weekday(self) -> Weekday:
        """Return the named weekday."""
        return tuple(Weekday)[(self.absolute_day - 1) % DAYS_PER_WEEK]

    @property
    def day_of_week(self) -> int:
        """Return the one-based day number within the week."""
        return self.weekday.number

    @property
    def is_decision_day(self) -> bool:
        """Return whether company Agents may act on this day."""
        return self.weekday.is_decision_day

    @property
    def is_settlement_day(self) -> bool:
        """Return whether weekly markets and consumer sales settle today."""
        return self.weekday is Weekday.SUNDAY

    @property
    def days_until_settlement(self) -> int:
        """Return whole days remaining until this week's Sunday settlement."""
        return DAYS_PER_WEEK - self.day_of_week

    def plus_days(self, days: int) -> Self:
        """Return a later simulation day without mutating this value."""
        if days < 0:
            raise ValueError("days must be non-negative")
        return type(self)(absolute_day=self.absolute_day + days)


class TradingCalendar(BaseModel):
    """Own episode bounds and valid decision-day navigation."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    weeks: int = Field(ge=1)

    @property
    def total_days(self) -> int:
        """Return the episode duration at day granularity."""
        return self.weeks * DAYS_PER_WEEK

    def contains(self, day: SimDay) -> bool:
        """Return whether a simulation day belongs to this episode."""
        return day.absolute_day <= self.total_days

    def days_of_week(self, week: int) -> tuple[SimDay, ...]:
        """Return Monday through Sunday for one configured week."""
        if not 1 <= week <= self.weeks:
            raise ValueError("week must belong to the calendar")
        monday = SimDay.at(week=week)
        return tuple(monday.plus_days(offset) for offset in range(DAYS_PER_WEEK))

    def next_decision_day(self, after: SimDay) -> SimDay | None:
        """Return the first later Monday-Saturday still inside the episode."""
        candidate = after.plus_days(1)
        while self.contains(candidate):
            if candidate.is_decision_day:
                return candidate
            candidate = candidate.plus_days(1)
        return None
