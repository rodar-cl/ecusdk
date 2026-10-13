"""Sesión ELM327 por TCP: reset, comando desconocido, VIN y DTCs."""

import socket
import threading
import time

from ecusdk.config import load_vehicle
from ecusdk.elm327 import Elm327Emulator, TcpElm327Server


def test_tcp_session_reset_unknown_command_vin_and_dtcs() -> None:
    vehicle = load_vehicle("examples/demo-car.toml")
    vehicle.start()
    vehicle.ecus[0].dtcs.add("P0302")
    server = TcpElm327Server(Elm327Emulator(vehicle), port=0)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    deadline = time.monotonic() + 2
    while server.port == 0 and time.monotonic() < deadline:
        time.sleep(0.001)
    try:
        with socket.create_connection(("127.0.0.1", server.port), timeout=2) as client:

            def command(value: str) -> str:
                client.sendall((value + "\r").encode())
                data = bytearray()
                while not data.endswith(b">"):
                    data.extend(client.recv(4096))
                return data.decode()

            atz = command("ATZ")
            assert "ELM327" in atz.upper()
            # Eco como un ELM327 real: el comando va en su propia línea.
            assert atz.split("\r")[:2] == ["ATZ", "ELM327 v1.5"]

            unknown = command("ATXYZ:123")
            assert "?" in unknown

            # La sesión sigue usable tras un comando desconocido: VIN y DTCs.
            assert "OK" in command("ATE0")
            assert "45 43 55 53 44 4B" in command("0902")
            assert command("03").startswith("43 01 03 02")
    finally:
        server.close()
        thread.join(timeout=2)
