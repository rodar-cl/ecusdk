"""Small, stdlib-only ELM327 emulator and transports."""

from __future__ import annotations

import os
import socket
import threading
from typing import Protocol

try:
    import termios
    import tty
except ImportError:  # pragma: no cover - only non-POSIX platforms
    termios = None
    tty = None

from ecusdk.can import CanFrame
from ecusdk.simulation import Vehicle


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
        return cls(socket.create_connection((host, port), timeout))

    def read(self, size: int = 4096) -> bytes:
        return self._socket.recv(size)

    def write(self, data: bytes) -> None:
        self._socket.sendall(data)

    def close(self) -> None:
        self._socket.close()


class PtySerialTransport:
    """Pseudo-terminal transport; unavailable platforms fail explicitly."""

    def __init__(self) -> None:
        if not hasattr(os, "openpty"):
            raise OSError("pseudo-terminal serial is unavailable on this platform")
        self.master, self.slave = os.openpty()
        self._closed = False
        try:
            if tty is not None and termios is not None:
                tty.setraw(self.slave, when=termios.TCSAFLUSH)
        except BaseException:
            self.close()
            raise

    @property
    def slave_name(self) -> str:
        return os.ttyname(self.slave)

    def read(self, size: int = 4096) -> bytes:
        try:
            return os.read(self.master, size)
        except OSError:
            if self._closed:
                return b""
            raise

    def write(self, data: bytes) -> None:
        os.write(self.master, data)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            os.close(self.slave)
        finally:
            os.close(self.master)


class Elm327Emulator:
    def __init__(self, vehicle: Vehicle, bus: str | None = None) -> None:
        self.vehicle = vehicle
        self.bus = bus or next(iter(vehicle.buses))
        self.echo = True
        self.headers = False
        self.protocol = "AUTO"

    def session(self) -> Elm327Emulator:
        """Create isolated AT state while retaining the shared vehicle."""
        return Elm327Emulator(self.vehicle, self.bus)

    def command(self, command: str) -> str:
        text = command.strip().upper().replace(" ", "")
        if text == "ATZ":
            self.echo, self.headers, self.protocol = True, False, "AUTO"
            return "ELM327 v1.5\r\r>"
        if text == "ATE0":
            self.echo = False
            return "OK\r\r>"
        if text == "ATE1":
            self.echo = True
            return "OK\r\r>"
        if text == "ATH0":
            self.headers = False
            return "OK\r\r>"
        if text == "ATH1":
            self.headers = True
            return "OK\r\r>"
        if text.startswith("ATSP"):
            self.protocol = text[4:] or "AUTO"
            return "OK\r\r>"
        if text == "ATDP":
            return f"{self.protocol}\r\r>"
        queries = {"010C", "010D", "03", "04", "0902"}
        if text in queries:
            ecu = self.vehicle.ecus[0]
            try:
                request = bytes.fromhex(text)
                frame = CanFrame(ecu.request_id, bytes((len(request),)) + request)
                payload = self.vehicle.exchange_isotp(self.bus, frame)
            except (ValueError, RuntimeError, KeyError):
                payload = None
            if payload is None:
                return "NO DATA\r\r>"
            result = payload.hex(" ").upper()
            if self.headers:
                result = f"{ecu.response_id:03X}  " + result
            return result + "\r\r>"
        return "?\r\r>"

    def serve(self, transport: ByteTransport) -> None:
        try:
            buffer = bytearray()
            while True:
                chunk = transport.read()
                if not chunk:
                    return
                buffer.extend(chunk)
                while b"\r" in buffer or b"\n" in buffer:
                    index = next(
                        i for i, value in enumerate(buffer) if value in (10, 13)
                    )
                    raw = bytes(buffer[:index])
                    del buffer[: index + 1]
                    if raw:
                        command = raw.decode("ascii", "replace")
                        response = (command if self.echo else "") + self.command(
                            command
                        )
                        transport.write(response.encode("ascii"))
        finally:
            transport.close()


class TcpElm327Server:
    """Blocking TCP server. Call ``close`` from the owning application."""

    def __init__(
        self, emulator: Elm327Emulator, host: str = "127.0.0.1", port: int = 0
    ) -> None:
        self.emulator, self.host, self.port = emulator, host, port
        self._socket: socket.socket | None = None
        self._stop = threading.Event()

    def serve_forever(self) -> None:
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
        self._stop.set()
        if self._socket is not None:
            self._socket.close()


Elm327Server = TcpElm327Server
