"""Generadores deterministas de señales.

Un generador es cualquier ``Callable[[float], SignalValue]`` que recibe los
segundos de ejecución del vehículo (sin contar pausas) y devuelve el valor de
la señal. Al ser funciones puras del tiempo, el resultado no depende de la
frecuencia de ``Vehicle.tick`` y una ejecución con la misma seed se repite.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, TypeAlias

from ecusdk.errors import ConfigurationError

SignalValue: TypeAlias = int | float
SignalGenerator: TypeAlias = Callable[[float], SignalValue]


def _finite(*values: float) -> None:
    if not all(math.isfinite(value) for value in values):
        raise ConfigurationError("los parámetros del generador deben ser finitos")


@dataclass(frozen=True, slots=True)
class Constant:
    """Valor fijo."""

    value: SignalValue

    def __post_init__(self) -> None:
        _finite(self.value)

    def __call__(self, elapsed: float) -> SignalValue:
        return self.value


@dataclass(frozen=True, slots=True)
class Linear:
    """``start + rate * elapsed``."""

    start: float
    rate: float

    def __post_init__(self) -> None:
        _finite(self.start, self.rate)

    def __call__(self, elapsed: float) -> SignalValue:
        return self.start + self.rate * elapsed


@dataclass(frozen=True, slots=True)
class Ramp:
    """Va de ``start`` a ``end`` en ``duration`` segundos desde ``at``."""

    start: float
    end: float
    duration: float
    at: float = 0.0

    def __post_init__(self) -> None:
        _finite(self.start, self.end, self.duration, self.at)
        if self.duration <= 0 or self.at < 0:
            raise ConfigurationError("la rampa requiere duration > 0 y at >= 0")

    def __call__(self, elapsed: float) -> SignalValue:
        progress = min(max((elapsed - self.at) / self.duration, 0.0), 1.0)
        return self.start + (self.end - self.start) * progress


@dataclass(frozen=True, slots=True)
class Sine:
    """``offset + amplitude * sin(2π * elapsed / period + phase)``."""

    offset: float
    amplitude: float
    period: float
    phase: float = 0.0

    def __post_init__(self) -> None:
        _finite(self.offset, self.amplitude, self.period, self.phase)
        if self.period <= 0:
            raise ConfigurationError("el período debe ser positivo")

    def __call__(self, elapsed: float) -> SignalValue:
        angle = 2 * math.pi * elapsed / self.period + self.phase
        return self.offset + self.amplitude * math.sin(angle)


@dataclass(frozen=True, slots=True)
class Random:
    """Valor uniforme en ``[minimum, maximum]``, nuevo cada ``interval``.

    Cada intervalo usa su propia semilla derivada de ``seed``, así que el
    valor sólo depende del tiempo transcurrido y no del número de ticks.
    """

    minimum: float
    maximum: float
    seed: int = 0
    interval: float = 1.0

    def __post_init__(self) -> None:
        _finite(self.minimum, self.maximum, self.interval)
        if self.minimum > self.maximum or self.interval <= 0:
            raise ConfigurationError(
                "random requiere minimum <= maximum e interval > 0"
            )

    def __call__(self, elapsed: float) -> SignalValue:
        step = math.floor(elapsed / self.interval)
        return random.Random(f"{self.seed}:{step}").uniform(self.minimum, self.maximum)


_KINDS: dict[str, type[Any]] = {
    "constant": Constant,
    "linear": Linear,
    "ramp": Ramp,
    "sine": Sine,
    "random": Random,
}


def generator_from_config(definition: Mapping[str, Any]) -> SignalGenerator:
    """Crea un generador desde una tabla TOML ``{type = "ramp", ...}``.

    Los generadores ``custom`` se registran por API con cualquier callable.
    """
    params = dict(definition)
    kind = params.pop("type", None)
    factory = _KINDS.get(kind) if isinstance(kind, str) else None
    if factory is None:
        raise ConfigurationError(f"tipo de generador desconocido: {kind!r}")
    try:
        return factory(**params)
    except TypeError as error:
        raise ConfigurationError(
            f"parámetros inválidos para {kind}: {error}"
        ) from error
