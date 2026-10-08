"""Servidor UDS (ISO 14229-1) mínimo: decoder, dispatcher y encoder.

Servicios: 0x10 DiagnosticSessionControl, 0x11 ECUReset, 0x19
ReadDTCInformation (0x01, 0x02, 0x0A), 0x22 ReadDataByIdentifier y 0x3E
TesterPresent. Los errores se responden con ``7F <SID> <NRC>``; una petición
con el bit suppressPosRspMsgIndication (0x80) en 0x10, 0x11 o 0x3E no
produce respuesta positiva.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import IntEnum
from typing import TypeAlias

from ecusdk.errors import UdsError
from ecusdk.obd import encode_dtc


class Nrc(IntEnum):
    """Negative Response Codes usados por el servidor."""

    SERVICE_NOT_SUPPORTED = 0x11
    SUB_FUNCTION_NOT_SUPPORTED = 0x12
    INCORRECT_MESSAGE_LENGTH = 0x13
    RESPONSE_TOO_LONG = 0x14
    CONDITIONS_NOT_CORRECT = 0x22
    REQUEST_OUT_OF_RANGE = 0x31


class DiagnosticSession(IntEnum):
    """Sesiones de DiagnosticSessionControl."""

    DEFAULT = 0x01
    PROGRAMMING = 0x02
    EXTENDED = 0x03


@dataclass(frozen=True, slots=True)
class DidSignal:
    """DID calculado desde una señal: ``round((valor - offset) * scale)``.

    Se codifica big-endian sin signo en ``length`` bytes. Un valor que no cabe
    produce NRC 0x22 (conditionsNotCorrect).
    """

    signal: str
    length: int = 2
    scale: float = 1.0
    offset: float = 0.0

    def __post_init__(self) -> None:
        if not self.signal or not 1 <= self.length <= 8:
            raise UdsError("DidSignal requiere signal y length entre 1 y 8")
        if not all(math.isfinite(v) for v in (self.scale, self.offset)) or (
            self.scale <= 0
        ):
            raise UdsError("scale debe ser positivo y offset finito")

    def encode(self, value: float) -> bytes | None:
        """Bytes del DID, o ``None`` si el valor no cabe en ``length``."""
        raw = round((value - self.offset) * self.scale)
        if not 0 <= raw < 1 << (8 * self.length):
            return None
        return raw.to_bytes(self.length, "big")


DidValue: TypeAlias = bytes | str | DidSignal

S3_SERVER = 5.0
"""Segundos sin peticiones antes de volver a la sesión por defecto."""

_P2_TIMINGS = bytes((0x00, 0x32, 0x01, 0xF4))  # P2 50 ms, P2* 5000 ms
_DTC_STATUS = 0x09  # testFailed | confirmedDTC
_STATUS_AVAILABILITY = 0x09
_SUPPRESSIBLE = frozenset({0x10, 0x11, 0x3E})


def negative_response(service: int, nrc: Nrc) -> bytes:
    """Construye ``7F <SID> <NRC>``."""
    return bytes((0x7F, service, nrc))


class _Negative(Exception):
    def __init__(self, nrc: Nrc) -> None:
        self.nrc = nrc


class UdsServer:
    """Estado UDS de una ECU: sesión, timer S3 y DIDs.

    ``signals`` se lee por referencia para los ``DidSignal``. ``dtcs`` devuelve
    los DTCs actuales. ``on_reset`` recibe el tipo de reset (0x01 hard,
    0x02 keyOffOn, 0x03 soft) después de confirmar la petición.
    """

    def __init__(
        self,
        signals: Mapping[str, int | float],
        dtcs: Callable[[], list[str]],
        on_reset: Callable[[int], None] | None = None,
    ) -> None:
        self._signals = signals
        self._dtcs = dtcs
        self._on_reset = on_reset
        self._dids: dict[int, DidValue] = {}
        self._session = DiagnosticSession.DEFAULT
        self._last_activity = 0.0
        self._services: dict[int, Callable[[bytes], bytes]] = {
            0x10: self._session_control,
            0x11: self._ecu_reset,
            0x19: self._read_dtc_information,
            0x22: self._read_data_by_identifier,
            0x3E: self._tester_present,
        }

    def did(self, identifier: int, value: DidValue) -> None:
        """Define o reemplaza un DID: bytes, texto ASCII o ``DidSignal``."""
        if type(identifier) is not int or not 0 <= identifier <= 0xFFFF:
            raise UdsError("el DID debe estar entre 0x0000 y 0xFFFF")
        if isinstance(value, str):
            try:
                value.encode("ascii")
            except UnicodeEncodeError as error:
                raise UdsError("los DIDs de texto deben ser ASCII") from error
        elif isinstance(value, DidSignal):
            if value.signal not in self._signals:
                raise UdsError(f"señal desconocida para DID: {value.signal}")
        # Validación en runtime para llamadas sin tipado estático.
        elif not isinstance(value, bytes):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise UdsError("valor de DID inválido")
        self._dids[identifier] = value

    def dids(self) -> dict[int, DidValue]:
        """Copia de los DIDs definidos."""
        return dict(self._dids)

    def session(self, now: float) -> DiagnosticSession:
        """Sesión activa en ``now``, aplicando el timeout S3."""
        if (
            self._session is not DiagnosticSession.DEFAULT
            and now - self._last_activity > S3_SERVER
        ):
            self._session = DiagnosticSession.DEFAULT
        return self._session

    def reset(self) -> None:
        """Vuelve a la sesión por defecto."""
        self._session = DiagnosticSession.DEFAULT

    def handle(self, request: bytes, now: float) -> bytes | None:
        """Respuesta a una petición UDS, o ``None`` si se suprime."""
        if not request:
            raise UdsError("petición UDS vacía")
        self.session(now)
        self._last_activity = now
        service = request[0]
        handler = self._services.get(service)
        if handler is None:
            return negative_response(service, Nrc.SERVICE_NOT_SUPPORTED)
        suppress = (
            service in _SUPPRESSIBLE and len(request) > 1 and bool(request[1] & 0x80)
        )
        try:
            response = handler(request)
        except _Negative as negative:
            return negative_response(service, negative.nrc)
        if len(response) > 4095:
            return negative_response(service, Nrc.RESPONSE_TOO_LONG)
        if service == 0x11 and self._on_reset is not None:
            self._on_reset(request[1] & 0x7F)
        return None if suppress else response

    @staticmethod
    def _sub_function(request: bytes, length: int) -> int:
        if len(request) != length:
            raise _Negative(Nrc.INCORRECT_MESSAGE_LENGTH)
        return request[1] & 0x7F

    def _session_control(self, request: bytes) -> bytes:
        sub = self._sub_function(request, 2)
        if sub not in DiagnosticSession._value2member_map_:
            raise _Negative(Nrc.SUB_FUNCTION_NOT_SUPPORTED)
        self._session = DiagnosticSession(sub)
        return bytes((0x50, sub)) + _P2_TIMINGS

    def _ecu_reset(self, request: bytes) -> bytes:
        sub = self._sub_function(request, 2)
        if sub not in (0x01, 0x02, 0x03):
            raise _Negative(Nrc.SUB_FUNCTION_NOT_SUPPORTED)
        self._session = DiagnosticSession.DEFAULT
        return bytes((0x51, sub))

    def _tester_present(self, request: bytes) -> bytes:
        if self._sub_function(request, 2) != 0x00:
            raise _Negative(Nrc.SUB_FUNCTION_NOT_SUPPORTED)
        return b"\x7e\x00"

    def _read_dtc_information(self, request: bytes) -> bytes:
        if len(request) < 2:
            raise _Negative(Nrc.INCORRECT_MESSAGE_LENGTH)
        sub = request[1] & 0x7F
        if sub not in (0x01, 0x02, 0x0A):
            raise _Negative(Nrc.SUB_FUNCTION_NOT_SUPPORTED)
        if len(request) != (2 if sub == 0x0A else 3):
            raise _Negative(Nrc.INCORRECT_MESSAGE_LENGTH)
        mask = 0xFF if sub == 0x0A else request[2]
        codes = self._dtcs() if _DTC_STATUS & mask else []
        header = bytes((0x59, sub, _STATUS_AVAILABILITY))
        if sub == 0x01:
            # formatIdentifier 0x01: ISO 14229-1 DTC format.
            return header + b"\x01" + len(codes).to_bytes(2, "big")
        records = (encode_dtc(code) + bytes((0x00, _DTC_STATUS)) for code in codes)
        return header + b"".join(records)

    def _read_data_by_identifier(self, request: bytes) -> bytes:
        if len(request) < 3 or len(request) % 2 == 0:
            raise _Negative(Nrc.INCORRECT_MESSAGE_LENGTH)
        response = bytearray((0x62,))
        for index in range(1, len(request), 2):
            identifier = int.from_bytes(request[index : index + 2], "big")
            if identifier not in self._dids:
                continue
            response += request[index : index + 2] + self._did_bytes(identifier)
        if len(response) == 1:
            raise _Negative(Nrc.REQUEST_OUT_OF_RANGE)
        return bytes(response)

    def _did_bytes(self, identifier: int) -> bytes:
        value = self._dids[identifier]
        if isinstance(value, str):
            return value.encode("ascii")
        if isinstance(value, bytes):
            return value
        signal = self._signals.get(value.signal)
        encoded = None if signal is None else value.encode(signal)
        if encoded is None:
            raise _Negative(Nrc.CONDITIONS_NOT_CORRECT)
        return encoded
