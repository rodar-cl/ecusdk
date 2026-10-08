"""Definición de fallos inyectables y su efecto sobre frames ISO-TP salientes.

Los fallos de frame se aplican a lo que emite una ECU, antes de llegar al bus,
y son funciones puras salvo ``frame_drop``, que usa el ``random.Random`` con
seed del vehículo. Los fallos de sensor y de disponibilidad los aplica
:class:`~ecusdk.simulation.Vehicle`.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

from ecusdk.can import CanFrame
from ecusdk.errors import SimulationError
from ecusdk.signals import SignalValue

AVAILABILITY_FAULTS = frozenset({"ecu_offline", "timeout"})
SENSOR_FAULTS = frozenset(
    {"sensor_frozen", "sensor_out_of_range", "intermittent_signal"}
)
DELAY_FAULTS = frozenset({"latency", "late_flow_control"})
FRAME_FAULTS = frozenset(
    {
        "frame_drop",
        "duplicate_frame",
        "malformed_frame",
        "invalid_length",
        "incorrect_pid",
        "missing_consecutive_frame",
        "wrong_sequence_number",
        "isotp_interruption",
    }
)
FAULT_KINDS = AVAILABILITY_FAULTS | SENSOR_FAULTS | DELAY_FAULTS | FRAME_FAULTS
FAULT_ALIASES = {"offline": "ecu_offline"}


@dataclass(frozen=True, slots=True)
class Fault:
    """Fallo activo sobre una ECU entre ``start`` y ``until`` (reloj del vehículo).

    Parámetros según ``kind``:

    - ``signal``: fallos de sensor; ``value`` es obligatorio para
      ``sensor_out_of_range`` y es el valor de caída de ``intermittent_signal``
      (0 por defecto). ``sensor_frozen`` congela el valor actual.
    - ``period``: semiperíodo de ``intermittent_signal``.
    - ``delay``: retardo de ``latency`` y ``late_flow_control``.
    - ``probability``: fracción de frames perdidos por ``frame_drop``.
    - ``sequence``: CF afectado por ``missing_consecutive_frame`` y
      ``wrong_sequence_number``.
    """

    kind: str
    ecu: str
    start: float
    until: float = math.inf
    signal: str | None = None
    value: SignalValue | None = None
    delay: float = 0.0
    probability: float = 1.0
    sequence: int = 1
    period: float = 1.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", FAULT_ALIASES.get(self.kind, self.kind))
        if self.kind not in FAULT_KINDS:
            raise SimulationError(f"fallo desconocido: {self.kind}")
        if self.until < self.start or math.isnan(self.until):
            raise SimulationError("la duración del fallo debe ser no negativa")
        if self.kind in SENSOR_FAULTS and not self.signal:
            raise SimulationError(f"{self.kind} requiere signal")
        if self.kind == "sensor_out_of_range" and self.value is None:
            raise SimulationError("sensor_out_of_range requiere value")
        if self.value is not None and not math.isfinite(self.value):
            raise SimulationError("value debe ser finito")
        if not math.isfinite(self.delay) or self.delay < 0:
            raise SimulationError("delay debe ser finito y no negativo")
        if not 0 <= self.probability <= 1:
            raise SimulationError("probability debe estar entre 0 y 1")
        if not 1 <= self.sequence <= 15:
            raise SimulationError("sequence debe estar entre 1 y 15")
        if not math.isfinite(self.period) or self.period <= 0:
            raise SimulationError("period debe ser positivo")

    def active(self, now: float) -> bool:
        """Indica si el fallo está vigente en ``now``."""
        return self.start <= now < self.until

    def frame_delay(self, frame: CanFrame) -> float:
        """Retardo aplicable a un frame saliente; 0 si no corresponde."""
        if self.kind == "latency" or (
            self.kind == "late_flow_control" and _pci(frame) == 3
        ):
            return self.delay
        return 0.0

    def apply(self, frame: CanFrame, rng: random.Random) -> list[CanFrame]:
        """Transforma un frame saliente según un fallo de ``FRAME_FAULTS``."""
        kind, pci, data = self.kind, _pci(frame), frame.data
        if not data:
            return [frame]
        if kind == "frame_drop":
            return [] if rng.random() < self.probability else [frame]
        if kind == "duplicate_frame":
            return [frame, frame]
        if kind == "malformed_frame":
            return [_replace(frame, bytes((0xF0 | data[0] & 0x0F,)) + data[1:])]
        if kind == "invalid_length":
            return [_replace(frame, data[:1])]
        if kind == "incorrect_pid" and pci == 0 and len(data) > 2 and data[1] == 0x41:
            return [
                _replace(frame, data[:2] + bytes(((data[2] + 1) & 0xFF,)) + data[3:])
            ]
        if pci == 2:
            sequence = data[0] & 0x0F
            if kind == "isotp_interruption" or (
                kind == "missing_consecutive_frame" and sequence == self.sequence
            ):
                return []
            if kind == "wrong_sequence_number" and sequence == self.sequence:
                wrong = 0x20 | (sequence + 1) & 0x0F
                return [_replace(frame, bytes((wrong,)) + data[1:])]
        return [frame]


def _pci(frame: CanFrame) -> int:
    return frame.data[0] >> 4 if frame.data else -1


def _replace(frame: CanFrame, data: bytes) -> CanFrame:
    return CanFrame(frame.arbitration_id, data, frame.is_extended_id, frame.timestamp)
