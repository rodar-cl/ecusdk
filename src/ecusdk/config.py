"""Carga de vehículos y escenarios desde TOML.

Los errores de configuración se informan como ``ConfigurationError``, que
también es ``ValueError``.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any, cast

from ecusdk.can import CanBus, SocketCanBus, VirtualCanBus
from ecusdk.errors import ConfigurationError, SimulationError, UdsError
from ecusdk.signals import generator_from_config
from ecusdk.simulation import ECU, Scenario, Vehicle
from ecusdk.uds import DidSignal, DidValue


def _did_value(raw: object, where: str) -> DidValue:
    """Texto ASCII, lista de bytes o tabla ``{signal, length, scale, offset}``."""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        items = cast(list[object], raw)
        if not all(type(item) is int and 0 <= item <= 0xFF for item in items):
            raise ConfigurationError(f"{where}: la lista debe contener bytes")
        return bytes(cast(list[int], items))
    try:
        return DidSignal(**_table(raw, where))
    except TypeError as error:
        raise ConfigurationError(f"{where}: {error}") from error


def _table(value: object, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigurationError(f"{where} debe ser una tabla")
    return cast(dict[str, Any], value)


def load_vehicle(path: str | Path) -> Vehicle:
    """Carga un vehículo; ``[scenario]`` y ``generator`` son opcionales.

    Ejemplo de escenario::

        [scenario]
        events = [
            { at = 5, ecu = "ecm", action = "set", signal = "rpm", value = 3000 },
            { at = 10, ecu = "ecm", action = "ecu_offline", duration = 2 },
        ]
    """
    with Path(path).open("rb") as file:
        try:
            document: dict[str, Any] = tomllib.load(file)
        except tomllib.TOMLDecodeError as error:
            raise ConfigurationError(f"TOML inválido: {error}") from error

    vehicle_data = _table(document.get("vehicle", {}), "[vehicle]")
    if not isinstance(vehicle_data.get("name"), str):
        raise ConfigurationError("[vehicle] requiere name")
    buses_data = _table(document.get("buses", {}), "[buses]")
    ecus_data = _table(document.get("ecus", {}), "[ecus]")
    buses: dict[str, CanBus] = {}
    for name, raw_definition in buses_data.items():
        definition = _table(raw_definition, f"la configuración del bus {name!r}")
        bus_type = definition.get("type", "can")
        if bus_type in ("can", "virtual"):
            buses[name] = VirtualCanBus()
        elif bus_type == "socketcan":
            interface = definition.get("interface")
            if definition.get("physical") is not True:
                raise ConfigurationError(
                    f"el bus físico {name!r} requiere physical = true"
                )
            if not isinstance(interface, str) or not interface:
                raise ConfigurationError(
                    f"el bus físico {name!r} requiere una interface explícita"
                )
            buses[name] = SocketCanBus(interface)
        else:
            raise ConfigurationError(f"tipo de bus desconocido: {bus_type!r}")
    ecus: list[ECU] = []
    for name, raw_data in ecus_data.items():
        data = _table(raw_data, f"la ECU {name!r}")
        can_data = _table(data.get("can", {}), f"[ecus.{name}.can]")
        signal_data = {
            signal: _table(definition, f"la señal {name}.{signal}")
            for signal, definition in _table(data.get("signals", {}), "signals").items()
        }
        if "request_id" not in can_data or "response_id" not in can_data:
            raise ConfigurationError(
                f"la ECU {name!r} requiere request_id y response_id"
            )
        try:
            ecu = ECU(
                name=name,
                request_id=can_data["request_id"],
                response_id=can_data["response_id"],
                signals={
                    signal: definition.get("initial", 0)
                    for signal, definition in signal_data.items()
                },
                signal_limits={
                    signal: (definition.get("min"), definition.get("max"))
                    for signal, definition in signal_data.items()
                },
                bus=str(data.get("bus")) if data.get("bus") is not None else None,
                vin=str(vehicle_data.get("vin", "")),
                generators={
                    signal: generator_from_config(
                        _table(definition["generator"], f"generator de {signal}")
                    )
                    for signal, definition in signal_data.items()
                    if "generator" in definition
                },
            )
        except (ValueError, KeyError) as error:
            if isinstance(error, ConfigurationError):
                raise
            raise ConfigurationError(f"ECU {name!r} inválida: {error}") from error
        ecus.append(ecu)
        obd_table = _table(data.get("obd", {}), f"la configuración OBD de {name!r}")
        mapped_pids: set[int] = set()
        for key, source in obd_table.items():
            if type(source) is not str:
                raise ConfigurationError(
                    "las asignaciones OBD deben ser PID y señal de texto"
                )
            match = re.fullmatch(r"([0-9A-Fa-f]{2}):([0-9A-Fa-f]{2})", key)
            if match is None:
                raise ConfigurationError(
                    f"clave OBD inválida: {key!r}; se esperaba '01:0C'"
                )
            mode, identifier = (int(part, 16) for part in match.groups())
            if mode != 0x01:
                raise ConfigurationError(
                    f"modo OBD no soportado en configuración: {mode:02X}"
                )
            if identifier in mapped_pids:
                raise ConfigurationError(
                    f"PID OBD duplicado en configuración: {identifier:02X}"
                )
            mapped_pids.add(identifier)
            try:
                ecu.obd.pid(identifier, source=source)
            except ValueError as error:
                raise ConfigurationError(str(error)) from error
        uds_table = _table(data.get("uds", {}), f"[ecus.{name}.uds]")
        for key, raw in _table(uds_table.get("dids", {}), "dids").items():
            where = f"DID {key!r} de {name!r}"
            try:
                identifier = int(key, 16)
                ecu.uds.did(identifier, _did_value(raw, where))
            except (ValueError, UdsError) as error:
                if isinstance(error, ConfigurationError):
                    raise
                raise ConfigurationError(f"{where} inválido: {error}") from error

    scenario_data = _table(document.get("scenario", {}), "[scenario]")
    raw_events = scenario_data.get("events", [])
    if not isinstance(raw_events, list):
        raise ConfigurationError("[scenario] events debe ser una lista de tablas")
    events = [
        _table(event, "un evento de escenario") for event in cast(list[Any], raw_events)
    ]
    try:
        scenario = Scenario(events) if events else None
    except (SimulationError, TypeError, ValueError) as error:
        raise ConfigurationError(f"escenario inválido: {error}") from error
    names = {ecu.name for ecu in ecus}
    if scenario is not None:
        for event in scenario.events:
            if event.ecu not in names:
                raise ConfigurationError(f"escenario con ECU desconocida: {event.ecu}")
    seed = vehicle_data.get("seed", 0)
    if type(seed) is not int:
        raise ConfigurationError("[vehicle] seed debe ser entero")
    return Vehicle(vehicle_data["name"], buses, ecus, scenario=scenario, seed=seed)
