"""Small, stdlib-only ELM327 emulator and transports."""

from __future__ import annotations

import importlib
import os
import re
import socket
import threading
from collections.abc import Callable
from typing import Protocol, cast

from ecusdk.can import CanFrame
from ecusdk.errors import ProtocolError
from ecusdk.simulation import Vehicle

OpenPty = Callable[[], tuple[int, int]]
TtyName = Callable[[int], str]
SetRaw = Callable[[int, int], None]

_openpty = cast(OpenPty | None, getattr(os, "openpty", None))
_ttyname = cast(TtyName | None, getattr(os, "ttyname", None))
try:
    _termios = importlib.import_module("termios")
    _tty = importlib.import_module("tty")
    _setraw = cast(SetRaw, getattr(_tty, "setraw"))
    _tcsa_flush = cast(int, getattr(_termios, "TCSAFLUSH"))
except (ImportError, AttributeError):  # pragma: no cover - non-POSIX platforms
    _setraw = None
    _tcsa_flush = None


class ByteTransport(Protocol):
    def read(self, size: int = 4096) -> bytes: ...
    def write(self, data: bytes) -> None: ...
    def close(self) -> None: ...


class TcpTransport:
    """Connected TCP byte transport."""

    def __init__(self, connection: socket.socket) -> None:
        self._socket = connection

    @classmethod
    def connect(cls, host: str, port: int, timeout: float = 5.0) -> TcpTransport:
        """Abre una conexión TCP de cliente."""
        return cls(socket.create_connection((host, port), timeout))

    def read(self, size: int = 4096) -> bytes:
        """Lee hasta ``size`` bytes; ``b''`` indica EOF."""
        return self._socket.recv(size)

    def write(self, data: bytes) -> None:
        """Envía todos los bytes."""
        self._socket.sendall(data)

    def close(self) -> None:
        """Cierra la conexión."""
        self._socket.close()


class PtySerialTransport:
    """Pseudo-terminal transport; unavailable platforms fail explicitly."""

    def __init__(self) -> None:
        if (
            _openpty is None
            or _ttyname is None
            or _setraw is None
            or _tcsa_flush is None
        ):
            raise OSError("pseudo-terminal serial is unavailable on this platform")
        self.master, self.slave = _openpty()
        self._ttyname = _ttyname
        self._closed = False
        try:
            _setraw(self.slave, _tcsa_flush)
        except BaseException:
            self.close()
            raise

    @property
    def slave_name(self) -> str:
        """Ruta del pseudo-terminal al que se conecta el cliente."""
        return self._ttyname(self.slave)

    def read(self, size: int = 4096) -> bytes:
        """Lee bytes del cliente; devuelve ``b''`` tras ``close``."""
        try:
            return os.read(self.master, size)
        except OSError:
            if self._closed:
                return b""
            raise

    def write(self, data: bytes) -> None:
        """Escribe bytes hacia el cliente."""
        os.write(self.master, data)

    def close(self) -> None:
        """Cierra ambos extremos del pseudo-terminal; es idempotente."""
        if self._closed:
            return
        self._closed = True
        try:
            os.close(self.slave)
        finally:
            os.close(self.master)


def serve_lines(transport: ByteTransport, handler: Callable[[str], str]) -> None:
    """Lee líneas terminadas en CR/LF, escribe ``handler(línea)`` y cierra al EOF."""
    try:
        buffer = bytearray()
        while True:
            chunk = transport.read()
            if not chunk:
                return
            buffer.extend(chunk)
            while b"\r" in buffer or b"\n" in buffer:
                index = next(i for i, value in enumerate(buffer) if value in (10, 13))
                raw = bytes(buffer[:index])
                del buffer[: index + 1]
                if raw:
                    response = handler(raw.decode("utf-8", "replace"))
                    transport.write(response.encode("utf-8"))
    finally:
        transport.close()


_REQUEST = re.compile(r"(?:[0-9A-F]{2}){1,7}")
_NO_OP_AT = re.compile(r"AT(?:L[01]|AT[0-2]|M[01]|CAF[01]|ST[0-9A-F]{2}|AL|NL)")


class Elm327Emulator:
    """Subconjunto ELM327 v1.5 sobre las ECUs de un bus del vehículo.

    Sin ``ATSH`` las consultas son funcionales: se envían a cada ECU del bus
    y se imprime una línea por ECU que responde. ``ATSH 7E0`` dirige las
    consultas sólo a la ECU con ese ``request_id``. ``timeout`` es la espera
    por respuesta de cada ECU, en segundos del reloj del vehículo.
    """

    def __init__(
        self, vehicle: Vehicle, bus: str | None = None, timeout: float = 0.2
    ) -> None:
        self.vehicle = vehicle
        self.bus = bus or next(iter(vehicle.buses))
        self.timeout = timeout
        self._defaults()

    def _defaults(self) -> None:
        self.echo = True
        self.headers = False
        self.spaces = True
        self.protocol = "AUTO"
        self.header: int | None = None

    def session(self) -> Elm327Emulator:
        """Create isolated AT state while retaining the shared vehicle."""
        return Elm327Emulator(self.vehicle, self.bus, self.timeout)

    def command(self, command: str) -> str:
        """Ejecuta un comando AT u OBD y devuelve la respuesta con prompt ``>``."""
        text = command.strip().upper().replace(" ", "")
        if text in ("ATZ", "ATWS", "ATD"):
            self._defaults()
            return "ELM327 v1.5\r\r>" if text != "ATD" else "OK\r\r>"
        if text == "ATI":
            return "ELM327 v1.5\r\r>"
        settings = {
            "ATE0": ("echo", False),
            "ATE1": ("echo", True),
            "ATH0": ("headers", False),
            "ATH1": ("headers", True),
            "ATS0": ("spaces", False),
            "ATS1": ("spaces", True),
        }
        if text in settings:
            setattr(self, *settings[text])
            return "OK\r\r>"
        if text.startswith("ATSP"):
            self.protocol = text[4:] or "AUTO"
            return "OK\r\r>"
        if text == "ATDP":
            return f"{self.protocol}\r\r>"
        if text.startswith("ATSH") and re.fullmatch(r"[0-9A-F]{3}", text[4:]):
            self.header = int(text[4:], 16)
            return "OK\r\r>"
        if _NO_OP_AT.fullmatch(text):
            return "OK\r\r>"
        if _REQUEST.fullmatch(text):
            return self._query(bytes.fromhex(text))
        return "?\r\r>"

    def _query(self, request: bytes) -> str:
        ecus = [
            ecu
            for ecu in self.vehicle.ecus
            if (ecu.bus is None or ecu.bus == self.bus)
            and (self.header is None or ecu.request_id == self.header)
        ]
        lines: list[str] = []
        failed = False
        for ecu in ecus:
            frame = CanFrame(ecu.request_id, bytes((len(request),)) + request)
            try:
                payload = self.vehicle.exchange_isotp(self.bus, frame, self.timeout)
            except ProtocolError:
                failed = True
                continue
            except (ValueError, RuntimeError, KeyError):
                payload = None
            if payload is None:
                continue
            separator = " " if self.spaces else ""
            line = (payload.hex(" ") if self.spaces else payload.hex()).upper()
            if self.headers:
                line = f"{ecu.response_id:03X}{separator * 2}{line}"
            lines.append(line)
        if not lines:
            lines.append("CAN ERROR" if failed else "NO DATA")
        return "\r".join(lines) + "\r\r>"

    def serve(self, transport: ByteTransport) -> None:
        """Atiende comandos de ``transport`` hasta EOF y luego lo cierra."""

        def handle(command: str) -> str:
            return (command if self.echo else "") + self.command(command)

        serve_lines(transport, handle)


class LineService(Protocol):
    """Servicio de líneas: ``session`` crea el estado de cada conexión."""

    def session(self) -> LineService: ...
    def serve(self, transport: ByteTransport) -> None: ...


class TcpServer:
    """Blocking TCP server; one thread per connection.

    Call ``close`` from the owning application.
    """

    def __init__(
        self, emulator: LineService, host: str = "127.0.0.1", port: int = 0
    ) -> None:
        self.emulator, self.host, self.port = emulator, host, port
        self._socket: socket.socket | None = None
        self._stop = threading.Event()

    def serve_forever(self) -> None:
        """Acepta conexiones hasta ``close``; bloquea el hilo que lo llama."""
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((self.host, self.port))
        server.listen()
        server.settimeout(0.2)
        self._socket = server
        self.port = int(server.getsockname()[1])
        try:
            while not self._stop.is_set():
                try:
                    connection, _ = server.accept()
                except TimeoutError:
                    continue
                except OSError:
                    if self._stop.is_set():
                        break
                    raise
                threading.Thread(
                    target=self.emulator.session().serve,
                    args=(TcpTransport(connection),),
                    daemon=True,
                ).start()
        finally:
            server.close()

    def close(self) -> None:
        """Detiene ``serve_forever`` y libera el socket de escucha."""
        self._stop.set()
        if self._socket is not None:
            self._socket.close()


TcpElm327Server = TcpServer
Elm327Server = TcpServer
