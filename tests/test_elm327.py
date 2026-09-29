import os
import select
import socket
import threading
import time

import pytest

from ecusdk.config import load_vehicle
from ecusdk.elm327 import Elm327Emulator, PtySerialTransport, TcpElm327Server


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
        assert "45 43 55 53 44 4B" in command("0902")
        assert "43 03 01" in command("03")
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
