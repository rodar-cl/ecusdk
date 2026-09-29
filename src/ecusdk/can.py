# pyright: reportPrivateUsage=false
"""Tipos y buses CAN virtual y Linux SocketCAN."""

from __future__ import annotations

import socket
import struct
import sys
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from queue import Empty, Queue
from typing import Protocol, TypeAlias, cast

from ecusdk.clock import Clock
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


@dataclass(frozen=True, slots=True)
class CanFilter:
    """Filtro CAN por máscara; ``is_extended_id=None`` acepta ambos formatos."""

    arbitration_id: int
    mask: int
    is_extended_id: bool | None = None

    def __post_init__(self) -> None:
        if (
            type(self.arbitration_id) is not int
            or not 0 <= self.arbitration_id <= _CAN_EFF_MASK
        ):
            raise ValueError("arbitration_id de filtro fuera de rango")
        if type(self.mask) is not int or not 0 <= self.mask <= _CAN_EFF_MASK:
            raise ValueError("mask de filtro fuera de rango")
        if self.is_extended_id is not None and type(self.is_extended_id) is not bool:
            raise ValueError("is_extended_id debe ser bool o None")
        if self.is_extended_id is False and self.arbitration_id > 0x7FF:
            raise ValueError("arbitration_id fuera de rango para CAN estándar")

    def matches(self, frame: CanFrame) -> bool:
        return (
            self.is_extended_id is None or self.is_extended_id == frame.is_extended_id
        ) and frame.arbitration_id & self.mask == self.arbitration_id & self.mask


_CLOSED = object()


class VirtualCanNode:
    """Endpoint independiente conectado a un ``VirtualCanBus``."""

    def __init__(
        self,
        bus: VirtualCanBus,
        filters: tuple[CanFilter, ...],
        receive_own_messages: bool,
    ) -> None:
        self._bus = bus
        self._frames: Queue[CanFrame | object] = Queue()
        self._filters = filters
        self._receive_own_messages = receive_own_messages
        self._closed = False

    def send(self, frame: CanFrame) -> None:
        """Publica un frame en el bus."""
        self._bus._send(frame, sender=self)

    def recv(self, timeout: float | None = None) -> CanFrame | None:
        with self._bus._lock:
            if self._closed:
                raise AdapterError("nodo CAN virtual cerrado")
            frames = self._frames
        try:
            frame = frames.get(timeout=timeout)
        except Empty:
            return None
        if frame is _CLOSED:
            frames.put(_CLOSED)
            raise AdapterError("nodo CAN virtual cerrado")
        with self._bus._lock:
            if self._closed:
                raise AdapterError("nodo CAN virtual cerrado")
        return cast(CanFrame, frame)

    def set_filters(self, filters: Iterable[CanFilter]) -> None:
        """Reemplaza los filtros; una secuencia vacía acepta todos los frames."""
        normalized = self._bus._validate_filters(filters)
        with self._bus._lock:
            self._ensure_open()
            self._filters = normalized

    def close(self) -> None:
        """Desconecta el nodo; la operación es idempotente."""
        with self._bus._lock:
            if not self._closed:
                self._closed = True
                self._bus._nodes.discard(self)
                self._frames.put(_CLOSED)

    def _ensure_open(self) -> None:
        if self._closed or self._bus._closed:
            raise AdapterError("nodo CAN virtual cerrado")


class VirtualCanBus:
    """Bus CAN en memoria, seguro para productores y consumidores concurrentes."""

    def __init__(self, *, clock: Clock | None = None) -> None:
        self._frames: Queue[CanFrame | object] = Queue()
        self._clock = clock
        self._nodes: set[VirtualCanNode] = set()
        self._lock = threading.RLock()
        self._closed = False

    @staticmethod
    def _validate_filters(filters: Iterable[CanFilter]) -> tuple[CanFilter, ...]:
        normalized = tuple(filters)
        if any(not isinstance(cast(object, item), CanFilter) for item in normalized):
            raise TypeError("filters debe contener sólo CanFilter")
        return normalized

    def connect(
        self,
        *,
        filters: Iterable[CanFilter] = (),
        receive_own_messages: bool = False,
    ) -> VirtualCanNode:
        """Crea un endpoint con cola propia y filtros de recepción."""
        if type(receive_own_messages) is not bool:
            raise ValueError("receive_own_messages debe ser bool")
        normalized = self._validate_filters(filters)
        with self._lock:
            if self._closed:
                raise AdapterError(
                    "bus CAN virtual cerrado; llame reset() para reabrirlo"
                )
            node = VirtualCanNode(self, normalized, receive_own_messages)
            self._nodes.add(node)
            return node

    def set_clock(self, clock: Clock | None) -> None:
        """Usa este reloj para sellar frames futuros sin timestamp."""
        with self._lock:
            self._clock = clock

    def send(self, frame: CanFrame) -> None:
        """Publica un frame y lo copia a cada cola receptora que corresponda."""
        self._send(frame)

    def _send(self, frame: CanFrame, sender: VirtualCanNode | None = None) -> None:
        if not isinstance(cast(object, frame), CanFrame):
            raise TypeError("frame debe ser CanFrame")
        with self._lock:
            if self._closed:
                raise AdapterError("bus CAN virtual cerrado")
            if sender is not None:
                sender._ensure_open()
            if frame.timestamp is None and self._clock is not None:
                frame = CanFrame(
                    frame.arbitration_id,
                    frame.data,
                    frame.is_extended_id,
                    self._clock.now(),
                )
            # Bus receive is a passive tap and retains its historical loopback behavior.
            self._frames.put(frame)
            for node in tuple(self._nodes):
                if node._closed or (node is sender and not node._receive_own_messages):
                    continue
                if not node._filters or any(
                    rule.matches(frame) for rule in node._filters
                ):
                    node._frames.put(frame)

    def recv(self, timeout: float | None = None) -> CanFrame | None:
        """Recibe del tap pasivo del hub o devuelve ``None`` al agotar timeout."""
        with self._lock:
            if self._closed:
                raise AdapterError("bus CAN virtual cerrado")
            frames = self._frames
        try:
            frame = frames.get(timeout=timeout)
        except Empty:
            return None
        if frame is _CLOSED:
            frames.put(_CLOSED)
            raise AdapterError("bus CAN virtual cerrado")
        with self._lock:
            if self._closed or frames is not self._frames:
                raise AdapterError("bus CAN virtual cerrado")
        return cast(CanFrame, frame)

    def close(self) -> None:
        """Cierra el hub y sus nodos; ``reset`` lo reabre con colas vacías."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._frames.put(_CLOSED)
            for node in tuple(self._nodes):
                node._closed = True
                node._frames.put(_CLOSED)
            self._nodes.clear()

    def reset(self) -> None:
        """Cierra endpoints actuales, descarta sus colas y reabre un hub vacío."""
        with self._lock:
            old_frames = self._frames
            old_frames.put(_CLOSED)
            for node in tuple(self._nodes):
                node._closed = True
                node._frames.put(_CLOSED)
            self._nodes.clear()
            self._frames = Queue()
            self._closed = False


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
