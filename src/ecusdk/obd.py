"""DTCs y codecs OBD-II mínimos."""

from __future__ import annotations

import math
from collections.abc import MutableMapping
from dataclasses import dataclass, field
from typing import cast

from ecusdk.errors import ObdError


@dataclass(slots=True)
class DtcStore:
    codes: set[str] = field(default_factory=lambda: set[str]())

    def add(self, code: str) -> None:
        """Añade un DTC como ``P0301``; ValueError si el formato es inválido."""
        _validate_dtc(code)
        self.codes.add(code.upper())

    def remove(self, code: str) -> None:
        """Quita un DTC si existe."""
        self.codes.discard(code.upper())

    def clear(self) -> None:
        """Borra todos los DTCs (Mode 04)."""
        self.codes.clear()

    def list(self) -> list[str]:
        """DTCs almacenados, ordenados."""
        return sorted(self.codes)


def _validate_dtc(code: str) -> None:
    if (
        len(code) != 5
        or code[0].upper() not in "PCBU"
        or code[1].upper() not in "0123"
        or any(digit.upper() not in "0123456789ABCDEF" for digit in code[2:])
    ):
        raise ValueError(f"DTC inválido: {code}")


def encode_dtc(code: str) -> bytes:
    _validate_dtc(code)
    prefix = "PCBU".index(code[0].upper())
    suffix = code[1:].upper()
    return bytes(
        (
            (prefix << 6) | int(suffix[0], 16) << 4 | int(suffix[1], 16),
            int(suffix[2:], 16),
        )
    )


def decode_dtc(data: bytes) -> str:
    if len(data) != 2:
        raise ValueError("un DTC requiere dos bytes")
    prefix = "PCBU"[(data[0] >> 6) & 0x03]
    return f"{prefix}{data[0] & 0x3F:02X}{data[1]:02X}"


def obd_request(mode: int, pid: int = 0) -> bytes:
    if not 0 <= mode <= 0xFF or not 0 <= pid <= 0xFF:
        raise ObdError("mode y pid deben ser bytes")
    return bytes((mode, pid))


@dataclass(frozen=True, slots=True)
class PidCodec:
    """Codec PID numérico inmutable; encode/decode usan sólo bytes del valor.

    El valor físico es ``raw / scale + offset``. Valores no finitos, fuera de
    rango o datos de longitud inválida producen ObdError. La cuantización
    redondea a la unidad codificada más cercana.
    """

    identifier: int
    length: int
    unit: str
    minimum: float
    maximum: float
    scale: float
    source: str
    offset: float = 0.0

    def __post_init__(self) -> None:
        values = (self.minimum, self.maximum, self.scale, self.offset)
        if (
            not 0 <= self.identifier <= 255
            or self.length <= 0
            or not all(math.isfinite(v) for v in values)
            or self.scale <= 0
            or self.minimum > self.maximum
        ):
            raise ObdError("definición de codec PID inválida")
        raw_min, raw_max = (self._raw(v) for v in (self.minimum, self.maximum))
        if (
            not math.isfinite(raw_max)
            or raw_min < 0
            or not math.isclose(raw_min, round(raw_min), abs_tol=1e-9)
            or not math.isclose(raw_max, round(raw_max), abs_tol=1e-6)
            or round(raw_max) >= 1 << (8 * self.length)
        ):
            raise ObdError("definición de codec PID inválida")

    def _raw(self, value: float) -> float:
        return (value - self.offset) * self.scale

    def encode(self, value: int | float) -> bytes:
        """Codifica un valor físico en sus bytes OBD."""
        if not math.isfinite(value) or not self.minimum <= value <= self.maximum:
            raise ObdError(f"valor fuera de rango para PID {self.identifier:02X}")
        return round(self._raw(value)).to_bytes(self.length, "big")

    def decode(self, data: bytes) -> float:
        """Decodifica los bytes de valor; valida longitud y rango."""
        if len(data) != self.length:
            raise ObdError("longitud de datos PID inválida")
        value = int.from_bytes(data, "big") / self.scale + self.offset
        if not self.minimum - 1e-9 <= value <= self.maximum + 1e-9:
            raise ObdError("valor PID fuera de rango")
        return value


RPM_PID = PidCodec(0x0C, 2, "rpm", 0, 16383.75, 4, "rpm")
SPEED_PID = PidCodec(0x0D, 1, "km/h", 0, 255, 1, "speed")
PID_CODECS = (RPM_PID, SPEED_PID)
_PERCENT = 255 / 100
# Fórmulas SAE J1979; sólo RPM y velocidad se asignan por defecto.
PID_CATALOG = (
    PidCodec(0x04, 1, "%", 0, 100, _PERCENT, "engine_load"),
    PidCodec(0x05, 1, "°C", -40, 215, 1, "coolant_temperature", -40),
    PidCodec(0x0B, 1, "kPa", 0, 255, 1, "intake_manifold_pressure"),
    RPM_PID,
    SPEED_PID,
    PidCodec(0x0F, 1, "°C", -40, 215, 1, "intake_air_temperature", -40),
    PidCodec(0x10, 2, "g/s", 0, 655.35, 100, "maf"),
    PidCodec(0x11, 1, "%", 0, 100, _PERCENT, "throttle_position"),
    PidCodec(0x2F, 1, "%", 0, 100, _PERCENT, "fuel_level"),
    PidCodec(0x42, 2, "V", 0, 65.535, 1000, "battery_voltage"),
    PidCodec(0x46, 1, "°C", -40, 215, 1, "ambient_air_temperature", -40),
    PidCodec(0x5C, 1, "°C", -40, 210, 1, "oil_temperature", -40),
)


class ObdRegistry:
    """Definiciones y asignaciones de PID Mode 01 propias de una ECU.

    Los codecs inmutables pueden compartirse. Las asignaciones son locales y
    el mapa de señales se conserva por referencia para reflejar sus cambios.
    """

    def __init__(self, signals: MutableMapping[str, int | float]) -> None:
        self._signals = signals
        self._codecs: dict[int, PidCodec] = {
            codec.identifier: codec for codec in PID_CATALOG
        }
        self._bindings: dict[int, str] = {
            codec.identifier: codec.source for codec in PID_CODECS
        }
        self._defaults: set[int] = set(self._bindings)

    def register(self, codec: PidCodec, *, replace: bool = False) -> None:
        """Registra un codec; los duplicados requieren ``replace=True``."""
        if not isinstance(cast(object, codec), PidCodec):
            raise TypeError("codec debe ser PidCodec")
        if codec.identifier in self._codecs and not replace:
            raise ObdError(f"ya existe codec para PID {codec.identifier:02X}")
        prior_source = self._bindings.get(codec.identifier)
        prior_default = codec.identifier in self._defaults
        if (
            prior_source is not None
            and prior_source not in self._signals
            and not prior_default
        ):
            raise ObdError(
                f"señal desconocida para PID {codec.identifier:02X}: {prior_source}"
            )
        self._codecs[codec.identifier] = codec
        if prior_source is not None:
            self._bindings[codec.identifier] = prior_source
        else:
            self._bindings.pop(codec.identifier, None)
        if prior_default:
            self._defaults.add(codec.identifier)
        else:
            self._defaults.discard(codec.identifier)

    def pid(self, identifier: int, *, source: str) -> PidCodec:
        """Asigna un PID registrado a una señal existente y devuelve su codec."""
        if type(identifier) is not int or not 0 <= identifier <= 0xFF:
            raise ValueError("el PID debe ser un byte")
        codec = self._codecs.get(identifier)
        if codec is None:
            raise ObdError(f"PID desconocido: {identifier:02X}")
        if type(source) is not str or not source:
            raise ValueError("source debe ser un nombre de señal no vacío")
        if source not in self._signals:
            raise ObdError(f"señal desconocida para PID {identifier:02X}: {source}")
        self._bindings[identifier] = source
        self._defaults.discard(identifier)
        return codec

    def bindings(self) -> dict[int, str]:
        """Copia de las asignaciones PID → señal activas."""
        return dict(self._bindings)

    def supported(self) -> list[int]:
        """PIDs que responden: asignados a una señal existente o por defecto."""
        return sorted(
            identifier
            for identifier, source in self._bindings.items()
            if source in self._signals or identifier in self._defaults
        )

    def _support_bitmap(self, base: int) -> bytes:
        bits = 0
        for identifier in self.supported():
            if base < identifier <= base + 0x20:
                bits |= 1 << (0x20 - (identifier - base))
            elif identifier > base + 0x20:
                bits |= 1
        return bytes((0x41, base)) + bits.to_bytes(4, "big")

    def encode(self, identifier: int) -> bytes | None:
        """Devuelve los bytes de respuesta Mode 01 para un PID asignado.

        Los PIDs 00, 20, 40… responden el bitmap de PIDs soportados.
        """
        if identifier % 0x20 == 0 and identifier not in self._codecs:
            if identifier and not any(i > identifier for i in self.supported()):
                return None
            return self._support_bitmap(identifier)
        codec = self._codecs.get(identifier)
        source = self._bindings.get(identifier)
        if codec is None or source is None:
            return None
        if source not in self._signals:
            if identifier in self._defaults:
                value = 0
            else:
                raise ObdError(f"señal desconocida para PID {identifier:02X}: {source}")
        else:
            value = self._signals[source]
        return bytes((0x41, identifier)) + codec.encode(value)
