"""Cliente real: python-OBD se conecta al ELM327 TCP y lee RPM, VIN y DTCs."""

import importlib
import threading
from typing import Any

from ecusdk.config import load_vehicle
from ecusdk.elm327 import Elm327Emulator, TcpServer

obd: Any = importlib.import_module("obd")  # sin tipos: Any para Pyright strict


def test_python_obd_connects_and_queries() -> None:
    vehicle = load_vehicle("examples/demo-car.toml")
    vehicle.start()
    vehicle.ecus[0].dtcs.add("P0301")
    # python-OBD recorta '0', '1' y '2' de los extremos del VIN; el del ejemplo
    # (ECUSDK00000000001) quedaría en "ECUSDK".
    vehicle.ecus[0].vin = "WVWZZZ1JZXW386759"
    server = TcpServer(Elm327Emulator(vehicle))
    server.bind()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    connection = obd.OBD(f"socket://127.0.0.1:{server.port}", fast=False, timeout=2)
    try:
        assert connection.is_connected()
        assert connection.protocol_id() == "6"
        assert connection.query(obd.commands.RPM).value.magnitude == 850
        assert connection.query(obd.commands.VIN).value == b"WVWZZZ1JZXW386759"
        assert connection.query(obd.commands.GET_DTC).value[0][0] == "P0301"
    finally:
        connection.close()
        server.close()
        vehicle.stop()
