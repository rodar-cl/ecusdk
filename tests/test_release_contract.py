"""Contrato público de v0.1 (PRD §§10, 19, 21, 54, 55 y 56).

Si un cambio rompe este archivo, es un cambio de API: actualízalo a propósito
y anótalo en el CHANGELOG.
"""

import inspect
import sys
import time
from collections.abc import Callable
from dataclasses import fields

import ecusdk
from ecusdk import (
    ECU,
    CanFrame,
    ECUSDKError,
    Scenario,
    Vehicle,
    VirtualCanBus,
)

PUBLIC_API = {
    "AdapterError",
    "CanBus",
    "CanError",
    "CanFilter",
    "CanFrame",
    "CanIdentifier",
    "Clock",
    "ConfigurationError",
    "Constant",
    "DtcStore",
    "ECU",
    "ECUSDKError",
    "Elm327Emulator",
    "Elm327Server",
    "FAULT_KINDS",
    "Fault",
    "IsoTpError",
    "IsoTpReceiver",
    "IsoTpSender",
    "IsoTpTimeoutError",
    "IsoTpTransport",
    "Linear",
    "ObdError",
    "ObdRegistry",
    "PID_CATALOG",
    "PidCodec",
    "ProtocolError",
    "PtySerialTransport",
    "RPM_PID",
    "Ramp",
    "Random",
    "RealClock",
    "SPEED_PID",
    "Scenario",
    "ScenarioEvent",
    "SignalGenerator",
    "SimulationError",
    "Sine",
    "SocketCanBus",
    "TcpElm327Server",
    "TcpServer",
    "TcpTransport",
    "UdsError",
    "Vehicle",
    "VehicleShell",
    "VehicleState",
    "VehicleStateError",
    "VirtualCanBus",
    "VirtualCanNode",
    "VirtualClock",
    "load_vehicle",
}


def test_public_names_are_stable_and_importable() -> None:
    assert set(ecusdk.__all__) == PUBLIC_API
    assert len(ecusdk.__all__) == len(PUBLIC_API)
    for name in PUBLIC_API:
        assert getattr(ecusdk, name) is not None


def signature(function: Callable[..., object]) -> str:
    return str(inspect.signature(function))


def test_core_signatures() -> None:
    assert [field.name for field in fields(CanFrame)] == [
        "arbitration_id",
        "data",
        "is_extended_id",
        "timestamp",
    ]
    assert [field.name for field in fields(ECU)][:10] == [
        "name",
        "request_id",
        "response_id",
        "signals",
        "vin",
        "dtcs",
        "online",
        "bus",
        "signal_limits",
        "generators",
    ]
    assert signature(Vehicle) == (
        "(name: 'str', buses: 'dict[str, CanBus]', ecus: 'list[ECU]', "
        "clock: 'Clock | None' = None, scenario: 'Scenario | None' = None, "
        "*, seed: 'int' = 0) -> 'None'"
    )
    assert signature(Vehicle.exchange) == (
        "(self, bus_name: 'str', request: 'CanFrame', timeout: 'float' = 0.0)"
        " -> 'CanFrame | None'"
    )
    assert signature(Scenario) == (
        "(events: 'Sequence[ScenarioEvent | dict[str, Any]]' = ()) -> 'None'"
    )
    assert signature(ecusdk.load_vehicle) == "(path: 'str | Path') -> 'Vehicle'"
    for method in ("start", "stop", "pause", "resume", "reset", "tick"):
        assert signature(getattr(Vehicle, method)) == "(self) -> 'None'"
    assert {"send", "recv"} <= set(dir(ecusdk.CanBus))
    assert {"now", "sleep"} <= set(dir(ecusdk.Clock))


def test_every_error_is_catchable_as_ecusdk_error() -> None:
    errors = [
        getattr(ecusdk, name)
        for name in PUBLIC_API
        if name.endswith("Error") and name != "ECUSDKError"
    ]
    assert len(errors) == 10
    assert all(issubclass(error, ECUSDKError) for error in errors)


def test_public_symbols_are_documented() -> None:
    missing: list[str] = []
    for name in PUBLIC_API:
        value = getattr(ecusdk, name)
        if not (inspect.isclass(value) or inspect.isfunction(value)):
            continue
        if not value.__doc__:
            missing.append(name)
        if not inspect.isclass(value):
            continue
        for member, attribute in vars(value).items():
            if isinstance(attribute, property):
                attribute = attribute.fget
            attribute = getattr(attribute, "__func__", attribute)
            if member.startswith("_") or not inspect.isfunction(attribute):
                continue
            if not attribute.__doc__:
                missing.append(f"{name}.{member}")
    assert missing == []


def test_twenty_ecus_sustain_thousands_of_frames_per_second() -> None:
    ecus = [ECU(f"ecu{i}", 0x700 + i, 0x780 + i, {"rpm": 850}) for i in range(20)]
    vehicle = Vehicle("bench", {"can": VirtualCanBus()}, ecus)
    vehicle.start()
    requests = [CanFrame(ecu.request_id, bytes.fromhex("02 01 0C")) for ecu in ecus]
    frames, started = 0, time.perf_counter()
    for index in range(2000):
        frames += 1 + len(vehicle.exchange_frames("can", requests[index % 20]))
    rate = frames / (time.perf_counter() - started)
    # ~22.000 frames/s en un M4. La medición de cobertura lo frena ~10x, así
    # que con ella sólo se exige un mínimo; Windows en CI corre sin cobertura.
    minimum = 200 if "coverage" in sys.modules else 2000
    assert rate > minimum, f"{rate:.0f} frames/s"
