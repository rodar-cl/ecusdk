"""Carga de vehículos desde TOML."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any, cast

from ecusdk.can import CanBus, SocketCanBus, VirtualCanBus
from ecusdk.simulation import ECU, Vehicle


def load_vehicle(path: str | Path) -> Vehicle:
    with Path(path).open("rb") as file:
        document: dict[str, Any] = tomllib.load(file)

    vehicle_data = document.get("vehicle", {})
    buses_data = document.get("buses", {})
    ecus_data = document.get("ecus", {})
    buses: dict[str, CanBus] = {}
    for name, raw_definition in buses_data.items():
        if not isinstance(raw_definition, dict):
            raise ValueError(f"la configuración del bus {name!r} debe ser una tabla")
        definition = cast(dict[str, Any], raw_definition)
        bus_type = definition.get("type", "can")
        if bus_type in ("can", "virtual"):
            buses[name] = VirtualCanBus()
        elif bus_type == "socketcan":
            interface = definition.get("interface")
            if definition.get("physical") is not True:
                raise ValueError(f"el bus físico {name!r} requiere physical = true")
            if not isinstance(interface, str) or not interface:
                raise ValueError(
                    f"el bus físico {name!r} requiere una interface explícita"
                )
            buses[name] = SocketCanBus(interface)
        else:
            raise ValueError(f"tipo de bus desconocido: {bus_type!r}")
    ecus: list[ECU] = []
    for name, data in ecus_data.items():
        can_data = data.get("can", {})
        signal_data = data.get("signals", {})
        signals = {
            signal_name: definition.get("initial", 0)
            for signal_name, definition in signal_data.items()
        }
        limits = {
            signal_name: (definition.get("min"), definition.get("max"))
            for signal_name, definition in signal_data.items()
        }
        ecus.append(
            ECU(
                name=name,
                request_id=can_data["request_id"],
                response_id=can_data["response_id"],
                signals=signals,
                signal_limits=limits,
                bus=str(data.get("bus")) if data.get("bus") is not None else None,
                vin=str(vehicle_data.get("vin", "")),
            )
        )
        obd_data = data.get("obd", {})
        if not isinstance(obd_data, dict):
            raise ValueError(f"la configuración OBD de {name!r} debe ser una tabla")
        ecu = ecus[-1]
        obd_table = cast(dict[str, Any], obd_data)
        mapped_pids: set[int] = set()
        for key, source in obd_table.items():
            if type(key) is not str or type(source) is not str:
                raise ValueError("las asignaciones OBD deben ser PID y señal de texto")
            match = re.fullmatch(r"([0-9A-Fa-f]{2}):([0-9A-Fa-f]{2})", key)
            if match is None:
                raise ValueError(f"clave OBD inválida: {key!r}; se esperaba '01:0C'")
            mode, identifier = (int(part, 16) for part in match.groups())
            if mode != 0x01:
                raise ValueError(f"modo OBD no soportado en configuración: {mode:02X}")
            if identifier in mapped_pids:
                raise ValueError(
                    f"PID OBD duplicado en configuración: {identifier:02X}"
                )
            mapped_pids.add(identifier)
            ecu.obd.pid(identifier, source=source)
    return Vehicle(vehicle_data["name"], buses, ecus)
