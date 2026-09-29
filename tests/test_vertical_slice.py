from ecusdk.can import CanFrame, VirtualCanBus
from ecusdk.clock import VirtualClock
from ecusdk.config import load_vehicle
from ecusdk.simulation import VehicleState


def test_virtual_clock_advances_deterministically() -> None:
    clock = VirtualClock()
    clock.advance(2.5)
    assert clock.now() == 2.5


def test_can_frame_rejects_too_much_data() -> None:
    try:
        CanFrame(0x7E0, b"123456789")
    except ValueError:
        pass
    else:
        raise AssertionError("un frame CAN no puede contener nueve bytes")


def test_virtual_bus_round_trip() -> None:
    bus = VirtualCanBus()
    frame = CanFrame(0x123, b"\x01")
    bus.send(frame)
    assert bus.recv(timeout=0) == frame


def test_demo_vehicle_answers_obd_rpm() -> None:
    vehicle = load_vehicle("examples/demo-car.toml")
    assert vehicle.state is VehicleState.STOPPED
    vehicle.start()
    response = vehicle.exchange(
        "powertrain",
        CanFrame(0x7E0, bytes.fromhex("02 01 0C")),
    )
    assert response is not None
    assert response.arbitration_id == 0x7E8
    assert response.data == bytes.fromhex("04 41 0C 0D 48")


def test_vehicle_uses_isotp_for_long_vin_and_dtcs() -> None:
    vehicle = load_vehicle("examples/demo-car.toml")
    vehicle.start()
    ecu = vehicle.ecus[0]
    ecu.dtcs.add("P0301")
    ecu.dtcs.add("U0100")

    vin = vehicle.exchange_isotp(
        "powertrain", CanFrame(0x7E0, bytes.fromhex("02 09 02"))
    )
    assert vin == bytes.fromhex("49 02 01") + b"ECUSDK00000000001"

    dtcs = vehicle.exchange_isotp("powertrain", CanFrame(0x7E0, bytes.fromhex("01 03")))
    assert dtcs == bytes.fromhex("43 03 01 C1 00")
