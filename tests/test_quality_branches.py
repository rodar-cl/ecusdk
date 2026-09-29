from pathlib import Path

import pytest

from ecusdk.can import CanFrame, SocketCanBus, VirtualCanBus
from ecusdk.clock import VirtualClock
from ecusdk.config import load_vehicle
from ecusdk.isotp import IsoTpReceiver, reassemble
from ecusdk.simulation import ECU, Scenario, ScenarioEvent, Vehicle


def test_protocol_and_config_error_branches(tmp_path: Path) -> None:
    with pytest.raises(Exception):
        reassemble([CanFrame(1, b"\x10")])
    receiver = IsoTpReceiver(clock=VirtualClock())
    with pytest.raises(Exception):
        receiver.push(CanFrame(1, b"\x10"))
    path = tmp_path / "bad.toml"
    path.write_text(
        '[vehicle]\nname = "v"\n[buses.bad]\ntype = "unknown"\n', encoding="utf-8"
    )
    with pytest.raises(ValueError):
        load_vehicle(path)
    path.write_text(
        '[vehicle]\nname = "v"\n[buses.bad]\ntype = "socketcan"\n', encoding="utf-8"
    )
    with pytest.raises(ValueError):
        load_vehicle(path)
    path.write_text(
        '[vehicle]\nname = "v"\n[buses.bad]\ntype = "socketcan"\nphysical = true\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_vehicle(path)


def test_vehicle_fault_and_scenario_errors() -> None:
    clock = VirtualClock()
    ecu = ECU("e", 1, 2, {"rpm": 0})
    vehicle = Vehicle("v", {"b": VirtualCanBus()}, [ecu], clock)
    vehicle.inject_timeout("e", 1)
    vehicle.set_ecu_offline("e", 1)
    vehicle.tick()
    scenario = Scenario([ScenarioEvent(0, "missing", "set", "rpm", 1)])
    vehicle = Vehicle("v", {"b": VirtualCanBus()}, [ecu], clock, scenario)
    vehicle.start()
    with pytest.raises(KeyError):
        vehicle.tick()


def test_socketcan_send_recv_wraps_os_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    class Failing:
        def bind(self, address: tuple[str, ...]) -> None:
            pass

        def close(self) -> None:
            pass

        def recv(self, size: int) -> bytes:
            raise OSError

        def sendall(self, data: bytes) -> None:
            raise OSError

        def settimeout(self, timeout: float | None) -> None:
            pass

    import ecusdk.can as can

    monkeypatch.setattr(can.socket, "AF_CAN", 29, raising=False)
    monkeypatch.setattr(can.socket, "CAN_RAW", 1, raising=False)
    bus = SocketCanBus("can0", socket_factory=lambda a, b, c: Failing())
    with pytest.raises(Exception):
        bus.send(CanFrame(1, b""))
    with pytest.raises(Exception):
        bus.recv()

    class TimeoutSocket(Failing):
        def recv(self, size: int) -> bytes:
            raise TimeoutError

    timeout_bus = SocketCanBus("can0", socket_factory=lambda a, b, c: TimeoutSocket())
    assert timeout_bus.recv() is None

    class CloseFail(Failing):
        def close(self) -> None:
            raise OSError

    close_bus = SocketCanBus("can0", socket_factory=lambda a, b, c: CloseFail())
    with pytest.raises(Exception):
        close_bus.close()
