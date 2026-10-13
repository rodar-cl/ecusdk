import os
import select
import socket
import threading
import time

import pytest

from ecusdk.can import VirtualCanBus
from ecusdk.clock import VirtualClock
from ecusdk.config import load_vehicle
from ecusdk.elm327 import (
    MAX_LINE,
    Elm327Emulator,
    PtySerialTransport,
    TcpElm327Server,
    TcpServer,
    serve_lines,
)
from ecusdk.simulation import ECU, Vehicle


def test_external_tcp_client_obd_and_settings() -> None:
    vehicle = load_vehicle("examples/demo-car.toml")
    vehicle.start()
    vehicle.ecus[0].dtcs.add("P0301")
    server = TcpElm327Server(Elm327Emulator(vehicle), port=0)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    deadline = time.monotonic() + 2
    while server.port == 0 and time.monotonic() < deadline:
        time.sleep(0.001)
    with socket.create_connection(("127.0.0.1", server.port), timeout=2) as client:

        def command(value: str) -> str:
            client.sendall((value + "\r").encode())
            data = bytearray()
            while not data.endswith(b">"):
                data.extend(client.recv(4096))
            return data.decode()

        assert "OK" in command("ATE0")
        assert "41 0C 0D 48" in command("010C")
        assert "0: 49 02 01 45 43 55\r1: 53 44 4B" in command("0902")
        assert "43 01 03 01" in command("03")
        assert "44" in command("04")
        assert command("03").startswith("43")
        assert "OK" in command("ATH1")
        assert "7E8" in command("010D")
    server.close()
    thread.join(timeout=2)
    assert not thread.is_alive()


def test_pty_serial_client_round_trip() -> None:
    vehicle = load_vehicle("examples/demo-car.toml")
    vehicle.start()
    try:
        transport = PtySerialTransport()
    except OSError:
        pytest.skip("pseudo-terminal serial is unavailable on this platform")
    client = os.open(transport.slave_name, os.O_RDWR | getattr(os, "O_NOCTTY", 0))
    thread = threading.Thread(target=Elm327Emulator(vehicle).serve, args=(transport,))
    thread.start()
    try:
        os.write(client, b"010C\r")
        ready, _, _ = select.select([client], [], [], 2)
        assert ready
        response = os.read(client, 4096)
        assert b"010C" in response
        assert b"41 0C 0D 48" in response
        assert response.endswith(b">")
    finally:
        os.close(client)
        transport.close()
        thread.join(timeout=2)
        vehicle.stop()
    assert not thread.is_alive()


def test_elm327_commands_used_by_real_clients() -> None:
    clock = VirtualClock()
    ecm = ECU("ecm", 0x7E0, 0x7E8, {"rpm": 850, "speed": 10})
    tcm = ECU("tcm", 0x7E1, 0x7E9, {"speed": 20})
    vehicle = Vehicle("v", {"bus": VirtualCanBus()}, [ecm, tcm], clock)
    vehicle.start()
    elm = Elm327Emulator(vehicle)
    assert elm.command("ATE0") == "OK\r\r>"
    assert elm.command("ATRV") == "12.6V\r\r>"
    assert elm.command("ATDPN") == "A6\r\r>"
    assert elm.command("ATSP6") == "OK\r\r>"
    assert elm.command("ATDPN") == "6\r\r>"
    assert elm.command("ATSPA8") == "OK\r\r>"
    assert elm.command("ATDP") == "AUTO, ISO 15765-4 (CAN 11/500)\r\r>"
    assert elm.command("ATSPXYZ") == "?\r\r>"
    assert elm.command("010D") == "41 0D 0A\r41 0D 14\r\r>"
    assert elm.command("010D1") == "41 0D 0A\r\r>"  # dígito final: 1 respuesta
    assert elm.command("ATSH7E1") == "OK\r\r>"
    assert elm.command("010D") == "41 0D 14\r\r>"
    assert elm.command("ATSH7DF") == "OK\r\r>"  # funcional, como sin ATSH
    assert elm.command("010D") == "41 0D 0A\r41 0D 14\r\r>"


def test_serve_lines_truncates_endless_lines() -> None:
    class Feed:
        def __init__(self, chunks: list[bytes]) -> None:
            self.chunks, self.written = chunks, list[bytes]()

        def read(self, size: int = 4096) -> bytes:
            return self.chunks.pop(0) if self.chunks else b""

        def write(self, data: bytes) -> None:
            self.written.append(data)

        def close(self) -> None:
            pass

    lines: list[str] = []
    feed = Feed([b"A" * 3000] * 3 + [b"\r010C\r"])
    serve_lines(feed, lambda line: lines.append(line) or "ok")
    assert lines == ["A" * MAX_LINE, "010C"]


def test_tcp_server_limits_clients_and_reports_busy_port() -> None:
    vehicle = load_vehicle("examples/demo-car.toml")
    vehicle.start()
    server = TcpServer(Elm327Emulator(vehicle), max_clients=1)
    server.bind()
    with pytest.raises(OSError):
        TcpServer(Elm327Emulator(vehicle), port=server.port).bind()
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        with socket.create_connection(("127.0.0.1", server.port), timeout=2) as first:
            first.sendall(b"ATI\r")
            assert first.recv(4096).startswith(b"ATI")
            with socket.create_connection(("127.0.0.1", server.port), 2) as second:
                assert second.recv(4096) == b""  # rechazado: sin cupo
    finally:
        server.close()
        thread.join(timeout=2)


def test_unanswered_query_waits_without_holding_the_vehicle_lock() -> None:
    vehicle = load_vehicle("examples/demo-car.toml")
    vehicle.start()
    elm = Elm327Emulator(vehicle, timeout=0.5)
    query = threading.Thread(target=elm.command, args=("01FF",))
    query.start()
    time.sleep(0.1)
    started = time.monotonic()
    with vehicle.lock:
        acquired = time.monotonic() - started
    query.join()
    assert acquired < 0.2
