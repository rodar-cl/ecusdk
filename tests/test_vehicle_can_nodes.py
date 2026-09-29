"""Tráfico observable del vehículo sobre nodos CAN independientes."""

from pathlib import Path

import pytest

from ecusdk import ECU, AdapterError, CanFrame, Vehicle, VirtualCanBus, VirtualClock
from ecusdk.config import load_vehicle


def rpm_request(identifier: int = 0x7E0) -> CanFrame:
    return CanFrame(identifier, bytes.fromhex("02 01 0C"))


def test_external_node_drives_ecu_without_queue_competition() -> None:
    clock = VirtualClock(20)
    bus = VirtualCanBus(clock=clock)
    tester, monitor = bus.connect(), bus.connect()
    ecu = ECU("ECM", 0x7E0, 0x7E8, {"rpm": 850}, bus="can")
    vehicle = Vehicle("v", {"can": bus}, [ecu], clock)
    vehicle.start()
    tester.send(rpm_request())
    vehicle.tick()
    reply = tester.recv(timeout=0)
    assert reply is not None and reply.data == bytes.fromhex("04 41 0C 0D 48")
    assert reply.timestamp == 20
    observed_request = monitor.recv(timeout=0)
    observed_reply = monitor.recv(timeout=0)
    assert observed_request is not None and observed_request.arbitration_id == 0x7E0
    assert observed_request.timestamp == 20
    assert observed_reply == reply
    assert tester.recv(timeout=0) is None


def test_convenience_exchange_broadcasts_without_consuming_monitor_frames() -> None:
    clock = VirtualClock()
    bus = VirtualCanBus()
    monitor = bus.connect()
    ecu = ECU("ECM", 0x7E0, 0x7E8, {"rpm": 3000})
    vehicle = Vehicle("v", {"can": bus}, [ecu], clock)
    vehicle.start()
    response = vehicle.exchange("can", rpm_request())
    assert response is not None and response.timestamp == 0
    assert response.data == bytes.fromhex("04 41 0C 2E E0")
    assert monitor.recv(timeout=0) is not None
    assert monitor.recv(timeout=0) == response
    assert monitor.recv(timeout=0) is None


def test_all_addressed_ecus_receive_broadcast_request() -> None:
    bus = VirtualCanBus()
    monitor = bus.connect()
    first = ECU("one", 0x7E0, 0x7E8, {"rpm": 100})
    second = ECU("two", 0x7E0, 0x7E9, {"rpm": 200})
    vehicle = Vehicle("v", {"can": bus}, [first, second], VirtualClock())
    vehicle.start()
    response = vehicle.exchange("can", rpm_request())
    assert response is not None and response.arbitration_id == 0x7E8
    observed: list[CanFrame] = []
    while (frame := monitor.recv(timeout=0)) is not None:
        observed.append(frame)
    assert [frame.arbitration_id for frame in observed] == [0x7E0, 0x7E8, 0x7E9]


def test_stop_start_does_not_replay_requests_sent_while_stopped() -> None:
    bus = VirtualCanBus()
    tester = bus.connect()
    vehicle = Vehicle("v", {"can": bus}, [ECU("e", 0x7E0, 0x7E8, {"rpm": 10})])
    vehicle.start()
    vehicle.stop()
    tester.send(rpm_request())
    vehicle.start()
    vehicle.tick()
    assert tester.recv(timeout=0) is None
    tester.send(rpm_request())
    vehicle.tick()
    assert tester.recv(timeout=0) is not None
    vehicle.reset()
    tester.send(rpm_request())
    vehicle.start()
    vehicle.tick()
    assert tester.recv(timeout=0) is None


def test_external_isotp_vin_honors_flow_control_over_nodes() -> None:
    bus = VirtualCanBus()
    tester = bus.connect()
    ecu = ECU("ECM", 0x7E0, 0x7E8, vin="ECUSDK00000000001")
    vehicle = Vehicle("v", {"can": bus}, [ecu], VirtualClock())
    vehicle.start()
    tester.send(CanFrame(0x7E0, bytes.fromhex("02 09 02")))
    vehicle.tick()
    first = tester.recv(timeout=0)
    assert first is not None and first.data[0] >> 4 == 1
    assert tester.recv(timeout=0) is None
    tester.send(CanFrame(0x7E0, bytes.fromhex("30 00 14")))
    vehicle.tick()
    assert tester.recv(timeout=0) is not None
    assert tester.recv(timeout=0) is None
    # Vehicle.tick must poll sender even when no new frame arrives.
    assert isinstance(vehicle.clock, VirtualClock)
    vehicle.clock.advance(0.020)
    vehicle.tick()
    final = tester.recv(timeout=0)
    assert final is not None and final.data[0] == 0x22


def test_frames_for_other_ids_do_not_block_diagnostic_exchange() -> None:
    bus = VirtualCanBus()
    other = bus.connect()
    vehicle = Vehicle("v", {"can": bus}, [ECU("e", 0x7E0, 0x7E8, {"rpm": 10})])
    vehicle.start()
    other.send(CanFrame(0x123, b"noise"))
    assert vehicle.exchange("can", rpm_request()) is not None


def test_stop_cancels_pending_cf_without_resetting_signals() -> None:
    clock = VirtualClock()
    bus = VirtualCanBus()
    tester = bus.connect()
    ecu = ECU("ECM", 0x7E0, 0x7E8, {"rpm": 850}, vin="ECUSDK00000000001")
    vehicle = Vehicle("v", {"can": bus}, [ecu], clock)
    vehicle.start()
    tester.send(CanFrame(0x7E0, bytes.fromhex("02 09 02")))
    vehicle.tick()
    assert tester.recv(timeout=0) is not None
    tester.send(CanFrame(0x7E0, bytes.fromhex("30 00 14")))
    vehicle.tick()
    assert tester.recv(timeout=0) is not None
    ecu.set_signal("rpm", 3000)
    vehicle.stop()
    clock.advance(0.020)
    vehicle.start()
    vehicle.tick()
    assert tester.recv(timeout=0) is None
    assert ecu.get_signal("rpm") == 3000


def test_generic_canbus_without_nodes_preserves_transaction_api() -> None:
    class LegacyBus:
        def __init__(self) -> None:
            self.bus = VirtualCanBus()

        def send(self, frame: CanFrame) -> None:
            self.bus.send(frame)

        def recv(self, timeout: float | None = None) -> CanFrame | None:
            return self.bus.recv(timeout)

    ecu = ECU("ECM", 0x7E0, 0x7E8, vin="ECUSDK00000000001")
    vehicle = Vehicle("v", {"legacy": LegacyBus()}, [ecu])
    vehicle.start()
    payload = vehicle.exchange_isotp(
        "legacy", CanFrame(0x7E0, bytes.fromhex("02 09 02"))
    )
    assert payload == bytes.fromhex("49 02 01") + ecu.vin.encode()


def test_failed_start_rolls_back_partial_connections() -> None:
    first, second = VirtualCanBus(), VirtualCanBus()
    monitor = first.connect()
    second.close()
    vehicle = Vehicle(
        "v", {"one": first, "two": second}, [ECU("e", 0x7E0, 0x7E8, {"rpm": 10})]
    )
    with pytest.raises(AdapterError):
        vehicle.start()
    second.reset()
    vehicle.start()
    assert vehicle.exchange("one", rpm_request()) is not None
    frames: list[CanFrame] = []
    while (frame := monitor.recv(timeout=0)) is not None:
        frames.append(frame)
    assert [frame.arbitration_id for frame in frames] == [0x7E0, 0x7E8]


def test_toml_binding_reaches_can_and_survives_vehicle_reset(tmp_path: Path) -> None:
    config = tmp_path / "bound.toml"
    config.write_text(
        '[vehicle]\nname="configured"\n[buses.can]\ntype="virtual"\n'
        '[ecus.ecm]\nbus="can"\n[ecus.ecm.can]\nrequest_id=2016\nresponse_id=2024\n'
        "[ecus.ecm.signals.rpm]\ninitial=10\n"
        "[ecus.ecm.signals.engine_rpm]\ninitial=850\n"
        '[ecus.ecm.obd]\n"01:0C"="engine_rpm"\n',
        encoding="utf-8",
    )
    vehicle = load_vehicle(config)
    vehicle.start()
    response = vehicle.exchange("can", rpm_request())
    assert response is not None and response.data == bytes.fromhex("04 41 0C 0D 48")
    vehicle.ecus[0].set_signal("engine_rpm", 3000)
    response = vehicle.exchange("can", rpm_request())
    assert response is not None and response.data == bytes.fromhex("04 41 0C 2E E0")
    vehicle.reset()
    vehicle.start()
    response = vehicle.exchange("can", rpm_request())
    assert response is not None and response.data == bytes.fromhex("04 41 0C 0D 48")
