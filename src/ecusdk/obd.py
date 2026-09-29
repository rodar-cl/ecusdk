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
        _validate_dtc(code)
        self.codes.add(code.upper())

    def remove(self, code: str) -> None:
        self.codes.discard(code.upper())

    def clear(self) -> None:
        self.codes.clear()

    def list(self) -> list[str]:
        return sorted(self.codes)


def _validate_dtc(code: str) -> None:
    if (
        len(code) != 5
        or code[0].upper() not in "PBCU"
        or code[1].upper() not in "0123"
        or any(digit.upper() not in "0123456789ABCDEF" for digit in code[2:])
    ):
        raise ValueError(f"DTC inválido: {code}")


def encode_dtc(code: str) -> bytes:
    _validate_dtc(code)
    prefix = "PBCU".index(code[0].upper())
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
    prefix = "PBCU"[(data[0] >> 6) & 0x03]
    return f"{prefix}{data[0] & 0x3F:02X}{data[1]:02X}"


def obd_request(mode: int, pid: int = 0) -> bytes:
    if not 0 <= mode <= 0xFF or not 0 <= pid <= 0xFF:
        raise ObdError("mode y pid deben ser bytes")
    return bytes((mode, pid))


@dataclass(frozen=True, slots=True)
class PidCodec:
    """Codec PID numérico inmutable; encode/decode usan sólo bytes del valor.

    Valores no finitos, fuera de rango o datos de longitud inválida producen
    ObdError. La cuantización redondea a la unidad codificada más cercana.
    """

    identifier: int
    length: int
    unit: str
    minimum: float
    maximum: float
    scale: float
    source: str

    def __post_init__(self) -> None:
        if (
            not 0 <= self.identifier <= 255
            or self.length <= 0
            or not all(
                math.isfinite(v) for v in (self.minimum, self.maximum, self.scale)
            )
            or self.scale <= 0
            or self.minimum < 0
            or self.minimum > self.maximum
            or not math.isfinite(self.maximum * self.scale)
            or self.minimum * self.scale != round(self.minimum * self.scale)
            or self.maximum * self.scale != round(self.maximum * self.scale)
            or round(self.maximum * self.scale) >= 1 << (8 * self.length)
        ):
            raise ObdError("definición de codec PID inválida")

    def encode(self, value: int | float) -> bytes:
        """Codifica un valor físico en sus bytes OBD."""
        if not math.isfinite(value) or not self.minimum <= value <= self.maximum:
            raise ObdError(f"valor fuera de rango para PID {self.identifier:02X}")
        return round(value * self.scale).to_bytes(self.length, "big")

    def decode(self, data: bytes) -> float:
        """Decodifica los bytes de valor; valida longitud y rango."""
        if len(data) != self.length:
            raise ObdError("longitud de datos PID inválida")
        value = int.from_bytes(data, "big") / self.scale
        if not self.minimum <= value <= self.maximum:
            raise ObdError("valor PID fuera de rango")
        return value


RPM_PID = PidCodec(0x0C, 2, "rpm", 0, 16383.75, 4, "rpm")
SPEED_PID = PidCodec(0x0D, 1, "km/h", 0, 255, 1, "speed")
PID_CODECS = (RPM_PID, SPEED_PID)


class ObdRegistry:
    """Definiciones y asignaciones de PID Mode 01 propias de una ECU.

    Los codecs inmutables pueden compartirse. Las asignaciones son locales y
    el mapa de señales se conserva por referencia para reflejar sus cambios.
    """

    def __init__(self, signals: MutableMapping[str, int | float]) -> None:
        self._signals = signals
        self._codecs: dict[int, PidCodec] = {
            codec.identifier: codec for codec in PID_CODECS
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

    def encode(self, identifier: int) -> bytes | None:
        """Devuelve los bytes de respuesta Mode 01 para un PID asignado."""
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
