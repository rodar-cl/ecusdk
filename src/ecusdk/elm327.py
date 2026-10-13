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
from ecusdk.isotp import reassemble
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


MAX_LINE = 1024
_EOL = re.compile(rb"[\r\n]")


def serve_lines(transport: ByteTransport, handler: Callable[[str], str]) -> None:
    """Lee líneas terminadas en CR/LF, escribe ``handler(línea)`` y cierra al EOF.

    Una línea de más de ``MAX_LINE`` bytes se entrega truncada y el resto se
    descarta, para que un cliente no pueda agotar la memoria.
    """

    def respond(raw: bytes) -> None:
        if raw:
            response = handler(raw.decode("utf-8", "replace"))
            transport.write(response.encode("utf-8"))

    try:
        buffer = bytearray()
        overflow = False
        while True:
            chunk = transport.read()
            if not chunk:
                return
            buffer.extend(chunk)
            while (match := _EOL.search(buffer)) is not None:
                raw = bytes(buffer[: match.start()])
                del buffer[: match.end()]
                if not overflow:
                    respond(raw)
                overflow = False
            if len(buffer) > MAX_LINE:
                if not overflow:
                    respond(bytes(buffer[:MAX_LINE]))
                buffer.clear()
                overflow = True
    finally:
        transport.close()


_REQUEST = re.compile(r"((?:[0-9A-F]{2}){1,7})([0-9A-F]?)")
_FUNCTIONAL = 0x7DF
# Los buses del simulador son siempre CAN 11 bits; el resto se acepta para
# que los clientes puedan fijar el protocolo como harían con un auto real.
_PROTOCOLS = {
    "1": "SAE J1850 PWM",
    "2": "SAE J1850 VPW",
    "3": "ISO 9141-2",
    "4": "ISO 14230-4 (KWP 5BAUD)",
    "5": "ISO 14230-4 (KWP FAST)",
    "6": "ISO 15765-4 (CAN 11/500)",
    "7": "ISO 15765-4 (CAN 29/500)",
    "8": "ISO 15765-4 (CAN 11/250)",
    "9": "ISO 15765-4 (CAN 29/250)",
    "A": "SAE J1939 (CAN 29/250)",
    "B": "USER1 (CAN 11/125)",
    "C": "USER2 (CAN 11/50)",
}
_NO_OP_AT = re.compile(r"AT(?:L[01]|AT[0-2]|M[01]|CAF[01]|ST[0-9A-F]{2}|AL|NL)")


class Elm327Emulator:
    """Subconjunto ELM327 v1.5 sobre las ECUs de un bus del vehículo.

    Sin ``ATSH`` (o con ``ATSH 7DF``) las consultas son funcionales: se envían
    a cada ECU del bus y se imprimen las respuestas de todas. ``ATSH 7E0``
    dirige las consultas sólo a la ECU con ese ``request_id``. Un dígito
    final (``010C1``) limita la cantidad de ECUs que responden. ``timeout`` es
    la espera por respuesta de cada ECU, en segundos del reloj del vehículo.
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
        self.protocol: str | None = None  # None: automático
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
        if text == "ATRV":
            return "12.6V\r\r>"
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
        if protocol := re.fullmatch(r"ATSPA?([0-9A-C])", text):
            auto = text.startswith("ATSPA") or protocol[1] == "0"
            self.protocol = None if auto else protocol[1]
            return "OK\r\r>"
        if text == "ATDP":
            if self.protocol is None:
                return f"AUTO, {_PROTOCOLS['6']}\r\r>"
            return f"{_PROTOCOLS[self.protocol]}\r\r>"
        if text == "ATDPN":
            return f"{self.protocol or 'A6'}\r\r>"
        if text.startswith("ATSH") and re.fullmatch(r"[0-9A-F]{3}", text[4:]):
            header = int(text[4:], 16)
            self.header = None if header == _FUNCTIONAL else header
            return "OK\r\r>"
        if _NO_OP_AT.fullmatch(text):
            return "OK\r\r>"
        if request := _REQUEST.fullmatch(text):
            limit = int(request[2], 16) if request[2] else 0
            return self._query(bytes.fromhex(request[1]), limit)
        return "?\r\r>"

    def _query(self, request: bytes, limit: int = 0) -> str:
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
            frames: list[CanFrame] = []
            try:
                frames = [
                    response
                    for response in self.vehicle.exchange_frames(
                        self.bus, frame, self.timeout
                    )
                    if response.arbitration_id == ecu.response_id
                ]
                payload = reassemble(frames) if frames else None
            except ProtocolError:
                failed = True
                continue
            except (ValueError, RuntimeError, KeyError):
                payload = None
            if payload is None:
                continue
            lines.extend(self._format(ecu.response_id, frames, payload))
            limit -= 1
            if limit == 0:
                break
        if not lines:
            lines.append("CAN ERROR" if failed else "NO DATA")
        return "\r".join(lines) + "\r\r>"

    def _format(
        self, response_id: int, frames: list[CanFrame], payload: bytes
    ) -> list[str]:
        """Formatea como un ELM327 con CAN 11 bits y formato automático (CAF1)."""
        separator = " " if self.spaces else ""

        def hexs(data: bytes) -> str:
            return (data.hex(" ") if self.spaces else data.hex()).upper()

        if self.headers:
            # Con cabeceras se muestra cada frame completo, incluido el PCI.
            return [f"{response_id:03X}{separator}{hexs(f.data)}" for f in frames]
        if len(frames) == 1:
            return [hexs(payload)]
        lines = [f"{len(payload):03X}"]
        for index, frame in enumerate(frames):
            data = frame.data[2:] if index == 0 else frame.data[1:]
            lines.append(f"{index & 0xF:X}:{separator}{hexs(data)}")
        return lines

    def serve(self, transport: ByteTransport) -> None:
        """Atiende comandos de ``transport`` hasta EOF y luego lo cierra."""

        def handle(command: str) -> str:
            return (command + "\r" if self.echo else "") + self.command(command)

        serve_lines(transport, handle)


class LineService(Protocol):
    """Servicio de líneas: ``session`` crea el estado de cada conexión."""

    def session(self) -> LineService: ...
    def serve(self, transport: ByteTransport) -> None: ...


class TcpServer:
    """Blocking TCP server; one thread per connection, up to ``max_clients``.

    Call ``close`` from the owning application.
    """

    def __init__(
        self,
        emulator: LineService,
        host: str = "127.0.0.1",
        port: int = 0,
        max_clients: int = 16,
    ) -> None:
        self.emulator, self.host, self.port = emulator, host, port
        self._socket: socket.socket | None = None
        self._stop = threading.Event()
        self._slots = threading.BoundedSemaphore(max_clients)

    def bind(self) -> None:
        """Abre el socket de escucha; ``port`` pasa a ser el puerto real.

        Llamarlo antes de ``serve_forever`` permite detectar un puerto ocupado
        en el hilo que arranca el servidor.
        """
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind((self.host, self.port))
            server.listen()
        except OSError:
            server.close()
            raise
        server.settimeout(0.2)
        self._socket = server
        self.port = int(server.getsockname()[1])

    def _serve_client(self, connection: socket.socket) -> None:
        try:
            self.emulator.session().serve(TcpTransport(connection))
        finally:
            self._slots.release()

    def serve_forever(self) -> None:
        """Acepta conexiones hasta ``close``; bloquea el hilo que lo llama."""
        if self._socket is None:
            self.bind()
        server = cast(socket.socket, self._socket)
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
                if not self._slots.acquire(blocking=False):
                    connection.close()  # demasiados clientes simultáneos
                    continue
                threading.Thread(
                    target=self._serve_client, args=(connection,), daemon=True
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
