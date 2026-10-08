import math

import pytest

from ecusdk import (
    ECU,
    CanFilter,
    CanFrame,
    Constant,
    IsoTpError,
    Linear,
    Ramp,
    Random,
    Scenario,
    ScenarioEvent,
    SimulationError,
    Sine,
    Vehicle,
    VehicleStateError,
    VirtualCanBus,
    VirtualClock,
)
from ecusdk.errors import ConfigurationError
from ecusdk.isotp import reassemble
from ecusdk.signals import generator_from_config

VIN = "ECUSDK00000000001"


def make_vehicle(
    scenario: Scenario | None = None, seed: int = 0
) -> tuple[Vehicle, VirtualClock, ECU]:
    clock = VirtualClock()
    ecu = ECU(
        "ecm",
        0x7E0,
        0x7E8,
        {"rpm": 850, "speed": 0},
        vin=VIN,
        signal_limits={"rpm": (0, 7000)},
    )
    vehicle = Vehicle("t", {"bus": VirtualCanBus()}, [ecu], clock, scenario, seed=seed)
    return vehicle, clock, ecu


def query(pid: int = 0x0C) -> CanFrame:
    return CanFrame(0x7E0, bytes((2, 0x01, pid)))


VIN_QUERY = CanFrame(0x7E0, bytes.fromhex("02 09 02"))


def test_generators_are_pure_functions_of_elapsed_time() -> None:
    assert Constant(5)(100) == 5
    assert Linear(10, 2)(3) == 16
    ramp = Ramp(0, 100, 10, at=5)
    assert (ramp(0), ramp(10), ramp(20)) == (0, 50, 100)
    assert Sine(10, 5, 4)(1) == pytest.approx(15)
    noise = Random(0, 1, seed=7, interval=0.5)
    assert noise(0.1) == noise(0.4) == Random(0, 1, seed=7, interval=0.5)(0.2)
    assert noise(0.1) != noise(0.6)
    assert 0 <= noise(3) <= 1
    assert generator_from_config({"type": "linear", "start": 1, "rate": 1})(1) == 2
    for bad in (
        {"type": "nope"},
        {"type": "ramp", "start": 0},
        {"type": "ramp", "start": 0, "end": 1, "duration": 0},
        {"type": "sine", "offset": 0, "amplitude": 1, "period": -1},
        {"type": "random", "minimum": 2, "maximum": 1},
        {"type": "constant", "value": math.nan},
    ):
        with pytest.raises(ConfigurationError):
            generator_from_config(bad)


def test_driven_signals_clamp_and_freeze_while_paused() -> None:
    vehicle, clock, ecu = make_vehicle()
    ecu.drive("rpm", Linear(1000, 1000))
    vehicle.start()
    vehicle.advance(2)
    assert ecu.get_signal("rpm") == 3000
    vehicle.pause()
    clock.advance(10)
    vehicle.resume()
    vehicle.tick()
    assert ecu.get_signal("rpm") == 3000
    vehicle.advance(10)
    assert ecu.get_signal("rpm") == 7000
    ecu.drive("rpm", lambda elapsed: 1234)
    vehicle.tick()
    assert ecu.get_signal("rpm") == 1234
    ecu.drive("rpm", None)
    ecu.set_signal("rpm", 900)
    vehicle.tick()
    assert ecu.get_signal("rpm") == 900
    with pytest.raises(KeyError):
        ecu.drive("missing", Constant(1))
    with pytest.raises(SimulationError):
        ecu.drive("rpm", 3)  # type: ignore[arg-type]  # validación en runtime
    with pytest.raises(SimulationError):
        vehicle.advance(-1)


def test_scenario_replay_is_deterministic() -> None:
    scenario = Scenario(
        [
            ScenarioEvent(0.5, "ecm", "set", "rpm", 2000),
            ScenarioEvent(1, "ecm", "frame_drop", options={"probability": 0.5}),
        ]
    )
    vehicle, _, ecu = make_vehicle(scenario, seed=42)
    ecu.drive("speed", Sine(50, 20, 2))

    def run() -> list[tuple[float, bytes | None]]:
        vehicle.start()
        trace: list[tuple[float, bytes | None]] = []
        for _ in range(40):
            vehicle.advance(0.1)
            response = vehicle.exchange("bus", query())
            trace.append((ecu.get_signal("speed"), response and response.data))
        return trace

    first = run()
    vehicle.reset()
    assert run() == first
    responses = [data for _, data in first]
    assert None in responses and any(responses)


def test_scenario_faults_use_scheduled_time_and_validate() -> None:
    scenario = Scenario(
        [
            {"at": 1, "ecu": "ecm", "action": "offline", "duration": 2},
            {"at": 4, "ecu": "ecm", "action": "latency", "delay": 0.5},
            {"at": 5, "ecu": "ecm", "action": "clear_fault"},
        ]
    )
    vehicle, clock, _ = make_vehicle(scenario)
    vehicle.start()
    clock.advance(2.5)
    assert vehicle.exchange("bus", query()) is None
    clock.advance(0.5)
    assert vehicle.exchange("bus", query()) is not None
    clock.advance(1)
    vehicle.tick()
    assert [fault.kind for fault in vehicle.faults()] == ["latency"]
    assert vehicle.faults()[0].delay == 0.5
    clock.advance(1)
    vehicle.tick()
    assert vehicle.faults() == []
    restored = Scenario.from_json(scenario.to_json())
    assert restored.events == scenario.events
    with pytest.raises(SimulationError):
        ScenarioEvent(0, "ecm", "explode")
    with pytest.raises(SimulationError):
        ScenarioEvent(-1, "ecm", "set")
    with pytest.raises(SimulationError):
        Scenario([{"at": 0, "ecu": "ecm", "action": "latency", "bogus": 1}])
    with pytest.raises(SimulationError):
        Scenario([{"at": 0, "action": "set"}])


def test_frame_faults_on_single_and_multi_frame_responses() -> None:
    vehicle, clock, _ = make_vehicle()
    vehicle.start()
    assert vehicle.exchange_isotp("bus", VIN_QUERY) == b"\x49\x02\x01" + VIN.encode()

    def faulted(
        kind: str, request: CanFrame = VIN_QUERY, probability: float = 1.0
    ) -> list[CanFrame]:
        vehicle.clear_fault("ecm")
        vehicle.inject_fault(kind, "ecm", probability=probability)
        return vehicle.exchange_frames("bus", request)

    assert faulted("frame_drop", query()) == []
    assert faulted("frame_drop", query(), probability=0.0) != []
    assert len(faulted("duplicate_frame", query())) == 2
    for kind in (
        "malformed_frame",
        "invalid_length",
        "missing_consecutive_frame",
        "wrong_sequence_number",
        "isotp_interruption",
        "duplicate_frame",
    ):
        vehicle.clear_fault("ecm")
        vehicle.advance(1.0)  # la ECU cancela por timeout el envío interrumpido
        vehicle.inject_fault(kind, "ecm")
        with pytest.raises(IsoTpError):
            vehicle.exchange_isotp("bus", VIN_QUERY)
    vehicle.clear_fault("ecm")
    vehicle.advance(1.0)
    vehicle.inject_fault("incorrect_pid", "ecm")
    assert vehicle.exchange_isotp("bus", query()) == bytes.fromhex("41 0D 0D 48")
    vehicle.clear_fault("ecm", "incorrect_pid")
    vehicle.inject_fault("missing_consecutive_frame", "ecm", sequence=2)
    started = clock.now()
    with pytest.raises(IsoTpError):
        vehicle.exchange_isotp("bus", VIN_QUERY, timeout=0.5)
    assert clock.now() == pytest.approx(started + 0.5)
    vehicle.clear_fault("ecm")
    vehicle.advance(1.0)
    assert vehicle.exchange_isotp("bus", VIN_QUERY) is not None


def test_latency_requires_waiting_and_expires() -> None:
    vehicle, clock, _ = make_vehicle()
    vehicle.start()
    vehicle.inject_fault("latency", "ecm", delay=0.3, duration=10)
    assert vehicle.exchange("bus", query()) is None
    vehicle.advance(0.3)  # la respuesta tardía llega después; se descarta aquí
    started = clock.now()
    payload = vehicle.exchange_isotp("bus", VIN_QUERY, timeout=2)
    assert payload == b"\x49\x02\x01" + VIN.encode()
    assert clock.now() - started == pytest.approx(0.6)  # FF y luego ambos CF
    clock.advance(10)
    assert vehicle.exchange("bus", query()) is not None
    with pytest.raises(ValueError):
        vehicle.exchange("bus", query(), timeout=-1)


def test_late_flow_control_delays_only_flow_control() -> None:
    vehicle, clock, _ = make_vehicle()
    vehicle.start()
    vehicle.inject_fault("late_flow_control", "ecm", delay=0.2)
    bus = vehicle.buses["bus"]
    assert isinstance(bus, VirtualCanBus)
    tester = bus.connect(filters=[CanFilter(0x7E8, 0x7FF, False)])
    tester.send(CanFrame(0x7E0, bytes.fromhex("10 08 01 0C 00 00 00 00")))
    vehicle.tick()
    assert tester.recv(timeout=0) is None
    clock.advance(0.2)
    vehicle.tick()
    control = tester.recv(timeout=0)
    assert control is not None and control.data[0] == 0x30
    tester.close()


def test_sensor_faults_override_observable_value() -> None:
    vehicle, clock, ecu = make_vehicle()
    vehicle.start()
    vehicle.inject_fault("sensor_frozen", "ecm", signal="rpm", duration=5)
    ecu.set_signal("rpm", 3000)
    assert ecu.get_signal("rpm") == 850
    clock.advance(5)
    vehicle.tick()
    assert ecu.get_signal("rpm") == 3000
    vehicle.inject_fault("sensor_out_of_range", "ecm", signal="rpm", value=20000)
    assert ecu.get_signal("rpm") == 20000
    assert vehicle.exchange("bus", query()) is None
    vehicle.clear_fault("ecm", "sensor_out_of_range")
    vehicle.inject_fault("intermittent_signal", "ecm", signal="rpm", period=1)
    clock.advance(0.5)
    vehicle.tick()
    assert ecu.get_signal("rpm") == 3000
    clock.advance(1)
    vehicle.tick()
    assert ecu.get_signal("rpm") == 0
    vehicle.reset()
    assert ecu.get_signal("rpm") == 850 and vehicle.faults() == []


def test_fault_validation() -> None:
    vehicle, _, _ = make_vehicle()
    cases: list[dict[str, object]] = [
        {"kind": "explode"},
        {"kind": "sensor_frozen"},
        {"kind": "sensor_out_of_range", "signal": "rpm"},
        {"kind": "latency", "delay": -1},
        {"kind": "frame_drop", "probability": 2},
        {"kind": "wrong_sequence_number", "sequence": 0},
        {"kind": "intermittent_signal", "signal": "rpm", "period": 0},
        {"kind": "timeout", "duration": -1},
        {"kind": "sensor_out_of_range", "signal": "rpm", "value": math.inf},
    ]
    for case in cases:
        options = dict(case)
        kind = str(options.pop("kind"))
        with pytest.raises(SimulationError):
            # Opciones heterogéneas a propósito para recorrer cada validación.
            vehicle.inject_fault(kind, "ecm", **options)  # type: ignore[arg-type]
    with pytest.raises(KeyError):
        vehicle.inject_fault("timeout", "missing")
    with pytest.raises(KeyError):
        vehicle.inject_fault("sensor_frozen", "ecm", signal="missing")
    assert vehicle.ecu("ECM").name == "ecm"
    vehicle.stop()
    with pytest.raises(VehicleStateError):
        vehicle.exchange("bus", query())
    with pytest.raises(RuntimeError):
        vehicle.exchange("bus", query())


def test_frame_faults_on_explicit_bus_path() -> None:
    class LoopbackBus:
        def __init__(self) -> None:
            self.frames: list[CanFrame] = []

        def send(self, frame: CanFrame) -> None:
            self.frames.append(frame)

        def recv(self, timeout: float | None = None) -> CanFrame | None:
            return self.frames.pop(0) if self.frames else None

    clock = VirtualClock()
    ecu = ECU("ecm", 0x7E0, 0x7E8, {"rpm": 850}, vin=VIN)
    vehicle = Vehicle("t", {"bus": LoopbackBus()}, [ecu], clock)
    vehicle.start()
    assert reassemble(vehicle.exchange_frames("bus", VIN_QUERY))[3:] == VIN.encode()
    vehicle.inject_fault("wrong_sequence_number", "ecm")
    with pytest.raises(IsoTpError):
        reassemble(vehicle.exchange_frames("bus", VIN_QUERY))
    vehicle.inject_fault("timeout", "ecm")
    assert vehicle.exchange_frames("bus", VIN_QUERY) == []
