import math

import pytest

from ecusdk import (
    ECU,
    CanFrame,
    Scenario,
    ScenarioEvent,
    Vehicle,
    VehicleState,
    VirtualCanBus,
    VirtualClock,
)


def make_vehicle(scenario: Scenario | None = None) -> tuple[Vehicle, VirtualClock, ECU]:
    clock = VirtualClock()
    ecu = ECU("ecm", 0x7E0, 0x7E8, {"rpm": 850, "speed": 0})
    return Vehicle("test", {"bus": VirtualCanBus()}, [ecu], clock, scenario), clock, ecu


def request() -> CanFrame:
    return CanFrame(0x7E0, bytes.fromhex("02 01 0C"))


def test_signal_mutation_and_validation() -> None:
    vehicle, _, ecu = make_vehicle()
    vehicle.start()
    ecu.set_signal("rpm", 3000)
    response = vehicle.exchange("bus", request())
    assert response is not None
    assert response.data == bytes.fromhex("04 41 0C 2E E0")
    with pytest.raises(KeyError):
        ecu.set_signal("unknown", 1)
    with pytest.raises(ValueError):
        ecu.set_signal("rpm", math.inf)


def test_scenario_pause_reset_and_repeatability() -> None:
    scenario = Scenario([ScenarioEvent(1, "ecm", "set", "rpm", 2000)])
    vehicle, clock, ecu = make_vehicle(scenario)
    vehicle.start()
    clock.advance(1)
    vehicle.tick()
    assert ecu.get_signal("rpm") == 2000
    vehicle.reset()
    assert vehicle.state is VehicleState.STOPPED
    assert ecu.get_signal("rpm") == 850
    vehicle.start()
    vehicle.pause()
    clock.advance(1)
    vehicle.tick()
    assert ecu.get_signal("rpm") == 850


def test_timeout_and_offline_restore_without_stale_response() -> None:
    vehicle, clock, _ = make_vehicle()
    vehicle.start()
    vehicle.inject_timeout("ecm", 2)
    assert vehicle.exchange("bus", request()) is None
    clock.advance(2)
    assert vehicle.exchange("bus", request()) is not None
    vehicle.set_ecu_offline("ecm", 2)
    clock.advance(1)
    assert vehicle.exchange("bus", request()) is None
    clock.advance(1)
    assert vehicle.exchange("bus", request()) is not None


def test_scenario_json_round_trip() -> None:
    scenario = Scenario([ScenarioEvent(2, "ecm", "set", "rpm", 1234)])
    restored = Scenario.from_json(scenario.to_json())
    vehicle, clock, ecu = make_vehicle(restored)
    vehicle.start()
    clock.advance(2)
    vehicle.tick()
    assert ecu.get_signal("rpm") == 1234


def test_multiple_ecus_are_assigned_to_their_bus() -> None:
    clock = VirtualClock()
    first = ECU("first", 0x7E0, 0x7E8, {"rpm": 100}, bus="one")
    second = ECU("second", 0x7E0, 0x7E9, {"rpm": 200}, bus="two")
    vehicle = Vehicle(
        "test", {"one": VirtualCanBus(), "two": VirtualCanBus()}, [first, second], clock
    )
    vehicle.start()
    first_response = vehicle.exchange("one", request())
    second_response = vehicle.exchange("two", request())
    assert first_response is not None
    assert second_response is not None
    assert first_response.arbitration_id == 0x7E8
    assert second_response.arbitration_id == 0x7E9
