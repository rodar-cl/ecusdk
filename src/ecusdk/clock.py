"""Relojes para simulaciones deterministas."""

from __future__ import annotations

import time
from typing import Protocol


class Clock(Protocol):
    def now(self) -> float: ...


class RealClock:
    def now(self) -> float:
        return time.monotonic()


class VirtualClock:
    def __init__(self, initial: float = 0.0) -> None:
        self._time = initial

    def now(self) -> float:
        return self._time

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("seconds no puede ser negativo")
        self._time += seconds
