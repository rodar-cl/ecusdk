"""Relojes para simulaciones deterministas."""

from __future__ import annotations

import math
import time
from typing import Protocol


class Clock(Protocol):
    """Fuente de tiempo monotónico en segundos."""

    def now(self) -> float: ...

    def sleep(self, seconds: float) -> None:
        """Espera ``seconds``; un reloj virtual avanza sin bloquear."""
        ...


def _check_seconds(seconds: float) -> None:
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError("seconds debe ser finito y no negativo")


class RealClock:
    """Reloj del sistema basado en ``time.monotonic``."""

    def now(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        _check_seconds(seconds)
        time.sleep(seconds)


class VirtualClock:
    """Reloj manual: el tiempo sólo avanza con ``advance`` o ``sleep``."""

    def __init__(self, initial: float = 0.0) -> None:
        self._time = initial

    def now(self) -> float:
        return self._time

    def advance(self, seconds: float) -> None:
        _check_seconds(seconds)
        self._time += seconds

    def sleep(self, seconds: float) -> None:
        self.advance(seconds)
