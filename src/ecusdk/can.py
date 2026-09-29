"""Tipos y buses CAN virtual y Linux SocketCAN."""

from __future__ import annotations

import socket
import struct
import sys
from collections.abc import Callable
from dataclasses import dataclass
from queue import Empty, Queue
from typing import Protocol, TypeAlias, cast

from ecusdk.errors import AdapterError

_CAN_FRAME = struct.Struct("=IB3x8s")
_CAN_EFF_FLAG = 0x80000000
_CAN_EFF_MASK = 0x1FFFFFFF


class _Socket(Protocol):
    def bind(self, address: tuple[str,]) -> None: ...

    def close(self) -> None: ...

    def recv(self, size: int) -> bytes: ...

    def sendall(self, data: bytes) -> None: ...

    def settimeout(self, timeout: float | None) -> None: ...


SocketFactory: TypeAlias = Callable[[int, int, int], _Socket]

CanIdentifier: TypeAlias = int


@dataclass(frozen=True, slots=True)
class CanFrame:
    """Frame CAN 2.0 con identificador y hasta ocho bytes de datos."""

    arbitration_id: CanIdentifier
    data: bytes
    is_extended_id: bool = False
    timestamp: float | None = None

    def __post_init__(self) -> None:
        # Normalize mutable bytearray inputs before publication of the frame.
        object.__setattr__(self, "data", bytes(self.data))
        maximum = 0x1FFFFFFF if self.is_extended_id else 0x7FF
        if not 0 <= self.arbitration_id <= maximum:
            raise ValueError("arbitration_id fuera de rango")
        if len(self.data) > 8:
            raise ValueError("un frame CAN 2.0 admite como máximo 8 bytes")


class CanBus(Protocol):
    def send(self, frame: CanFrame) -> None: ...

    def recv(self, timeout: float | None = None) -> CanFrame | None: ...


class VirtualCanBus:
    """Bus CAN en memoria, seguro para productores y consumidores concurrentes."""

    def __init__(self) -> None:
        self._frames: Queue[CanFrame] = Queue()

    def send(self, frame: CanFrame) -> None:
        self._frames.put(frame)

    def recv(self, timeout: float | None = None) -> CanFrame | None:
        try:
            return self._frames.get(timeout=timeout)
        except Empty:
            return None


class SocketCanBus:
    """Adaptador CAN_RAW para una interfaz Linux SocketCAN explícita.

    La interfaz se abre sólo cuando el llamador la proporciona; no hay
    selección automática de interfaces físicas.
    """

    def __init__(
        self,
        interface: str,
        *,
        socket_factory: SocketFactory | None = None,
    ) -> None:
        if not interface:
            raise ValueError("interface no puede estar vacía")
        if sys.platform != "linux" and socket_factory is None:
            raise AdapterError("SocketCAN sólo está disponible en Linux")
        factory = socket_factory or cast(SocketFactory, socket.socket)
        af_can = getattr(socket, "AF_CAN", None)
        raw = getattr(socket, "SOCK_RAW", None)
        can_raw = getattr(socket, "CAN_RAW", None)
        if af_can is None or can_raw is None or raw is None:
            raise AdapterError("esta plataforma no soporta SocketCAN")
        sock: _Socket | None = None
        try:
            sock = factory(af_can, raw, can_raw)
            sock.bind((interface,))
            self._socket = sock
        except (OSError, ValueError) as error:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
            raise AdapterError(f"no se pudo abrir SocketCAN {interface!r}") from error

    @staticmethod
    def _pack(frame: CanFrame) -> bytes:
        can_id = frame.arbitration_id
        if frame.is_extended_id:
            can_id |= _CAN_EFF_FLAG
        return _CAN_FRAME.pack(can_id, len(frame.data), frame.data.ljust(8, b"\0"))

    @staticmethod
    def _unpack(raw: bytes) -> CanFrame:
        if len(raw) != _CAN_FRAME.size:
            raise AdapterError("frame SocketCAN truncado")
        can_id, dlc, data = _CAN_FRAME.unpack(raw)
        if dlc > 8:
            raise AdapterError("DLC SocketCAN inválido")
        extended = bool(can_id & _CAN_EFF_FLAG)
        identifier = can_id & (_CAN_EFF_MASK if extended else 0x7FF)
        return CanFrame(identifier, data[:dlc], is_extended_id=extended)

    def send(self, frame: CanFrame) -> None:
        try:
            self._socket.sendall(self._pack(frame))
        except OSError as error:
            raise AdapterError("falló el envío por SocketCAN") from error

    def recv(self, timeout: float | None = None) -> CanFrame | None:
        try:
            self._socket.settimeout(timeout)
            return self._unpack(self._socket.recv(_CAN_FRAME.size))
        except TimeoutError:
            return None
        except OSError as error:
            raise AdapterError("falló la recepción por SocketCAN") from error

    def close(self) -> None:
        try:
            self._socket.close()
        except OSError as error:
            raise AdapterError("falló el cierre de SocketCAN") from error
