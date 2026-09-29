import math

import pytest

from ecusdk.can import CanFrame, SocketCanBus, VirtualCanBus
from ecusdk.clock import VirtualClock
from ecusdk.config import load_vehicle
from ecusdk.elm327 import Elm327Emulator
from ecusdk.errors import ObdError
from ecusdk.isotp import IsoTpReceiver, reassemble, segment
from ecusdk.obd import DtcStore, decode_dtc, encode_dtc, obd_request
from ecusdk.simulation import ECU, Scenario, ScenarioEvent, Vehicle, VehicleState


def test_can_validation_and_virtual_timeout() -> None:
    with pytest.raises(ValueError):
        CanFrame(-1, b"")
    with pytest.raises(ValueError):
        CanFrame(0x800, b"")
    with pytest.raises(ValueError):
        CanFrame(0, b"123456789")
    with pytest.raises(ValueError):
        VirtualClock().advance(-1)
    bus = VirtualCanBus()
    assert bus.recv(timeout=0) is None
    frame = CanFrame(0x123, b"x")
    bus.send(frame)
    assert bus.recv() == frame


def test_socketcan_validation() -> None:
    with pytest.raises(ValueError):
        SocketCanBus("")


def test_isotp_rejects_malformed_inputs() -> None:
    with pytest.raises(Exception):
        segment(b"x" * 4096, 1)
    with pytest.raises(Exception):
        reassemble([])
    with pytest.raises(Exception):
        reassemble([CanFrame(1, b"")])
    with pytest.raises(Exception):
        reassemble([CanFrame(1, b"\x01"), CanFrame(1, b"\x00")])
    receiver = IsoTpReceiver(clock=VirtualClock())
    with pytest.raises(Exception):
        receiver.push(CanFrame(1, b""))
    with pytest.raises(Exception):
        receiver.push(CanFrame(1, b"\x21x"))


def test_obd_validation_and_store() -> None:
    with pytest.raises(ObdError):
        obd_request(-1)
    with pytest.raises(ObdError):
        obd_request(1, 256)
    with pytest.raises(ValueError):
        encode_dtc("bad")
    with pytest.raises(ValueError):
        decode_dtc(b"x")
    store = DtcStore()
    store.add("p0301")
    store.remove("P0301")
    assert store.list() == []


def test_signal_and_ecu_validation() -> None:
    ecu = ECU("e", 1, 2, {"rpm": 100}, signal_limits={"rpm": (0, 1000)})
    with pytest.raises(ValueError):
        ecu.set_signal("rpm", math.inf)
    with pytest.raises(ValueError):
        ecu.set_signal("rpm", 1001)
    with pytest.raises(KeyError):
        ecu.get_signal("missing")
    assert ecu.obd_payload(b"\x01\x0d") == bytes.fromhex("410d00")
    assert ecu.obd_payload(b"\x7f") is None
    assert ecu.handle(CanFrame(99, b"\x01\x00")) is None
    ecu.online = False
    assert ecu.handle(CanFrame(1, b"\x01\x00")) is None


def test_scenario_pause_serialization_and_vehicle_controls() -> None:
    clock = VirtualClock()
    ecu = ECU("e", 1, 2, {"rpm": 0})
    scenario = Scenario([ScenarioEvent(1, "e", "set", "rpm", 500)])
    vehicle = Vehicle("v", {"b": VirtualCanBus()}, [ecu], clock, scenario)
    vehicle.pause()
    vehicle.start()
    clock.advance(1)
    vehicle.pause()
    vehicle.tick()
    assert ecu.get_signal("rpm") == 0
    vehicle.resume()
    vehicle.tick()
    assert ecu.get_signal("rpm") == 500
    assert Scenario.from_json(scenario.to_json()).events == scenario.events
    vehicle.stop()
    with pytest.raises(RuntimeError):
        vehicle.exchange("b", CanFrame(1, b""))
    vehicle.reset()
    assert vehicle.state is VehicleState.STOPPED


def test_config_and_elm_unknown_commands() -> None:
    vehicle = load_vehicle("examples/demo-car.toml")
    emulator = Elm327Emulator(vehicle)
    assert emulator.command("ATDP").startswith("AUTO")
    assert emulator.command("ATE0").startswith("OK")
    assert emulator.command("ATH1").startswith("OK")
    assert emulator.command("ATSP6").startswith("OK")
    assert emulator.command("ATDP").startswith("6")
    assert emulator.command("NOPE").startswith("?")
