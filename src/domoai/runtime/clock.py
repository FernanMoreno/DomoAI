"""Shared time source for runtime decisions."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FixedClock:
    def __init__(self, initial: datetime) -> None:
        self._current = initial

    def now(self) -> datetime:
        return self._current

    def set(self, value: datetime) -> None:
        self._current = value
