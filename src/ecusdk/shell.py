"""Shell de control de un vehículo: comandos de texto sobre la API pública.

``VehicleShell.execute`` interpreta una línea; ``serve`` la expone por TCP con
``TcpServer`` para que otro proceso (``ecusdk set``, ``ecusdk shell``) controle
un ``ecusdk run`` en ejecución. Cada conexión envía comandos y recibe una
respuesta por línea hasta cerrar su escritura.
"""

from __future__ import annotations

import math
import re
import socket
from collections.abc import Callable

from ecusdk.elm327 import ByteTransport, serve_lines
from ecusdk.errors import ECUSDKError
from ecusdk.faults import FAULT_KINDS
from ecusdk.simulation import Vehicle

HELP = """\
list ecus
inspect
get ECU.signal
set ECU.signal VALUE
dtc ECU add|remove CODE
dtc ECU clear|list
fault ECU KIND [DURATION] [signal=S] [value=V] [delay=S] [probability=P]
      [sequence=N] [period=S]
fault ECU clear [KIND]
faults [ECU]
pause | resume | reset
help"""

_DURATION = re.compile(r"(\d+(?:\.\d*)?|\.\d+)(ms|s)?")


def parse_duration(text: str) -> float:
    """Convierte ``500ms``, ``2s`` o ``1.5`` en segundos."""
    match = _DURATION.fullmatch(text)
    if match is None:
        raise ValueError(f"duración inválida: {text}")
    value = float(match.group(1))
    return value / 1000 if match.group(2) == "ms" else value


def _number(text: str) -> int | float:
    try:
        return int(text, 0)
    except ValueError:
        value = float(text)
        if not math.isfinite(value):
            raise ValueError(f"número inválido: {text}") from None
        return value


def describe(vehicle: Vehicle) -> str:
    """Resumen legible de buses, ECUs, señales, PIDs, DTCs y fallos activos."""
    first_vin = vehicle.ecus[0].vin if vehicle.ecus else ""
    lines = [f"vehicle {vehicle.name} ({vehicle.state.value})"]
    if first_vin:
        lines.append(f"  vin {first_vin}")
    for name, bus in vehicle.buses.items():
        lines.append(f"  bus {name}: {type(bus).__name__}")
    for ecu in vehicle.ecus:
        lines.append(
            f"  ecu {ecu.name}: request 0x{ecu.request_id:03X}"
            f" response 0x{ecu.response_id:03X} bus {ecu.bus or '*'}"
        )
        for signal, value in ecu.signals.items():
            driven = " (generator)" if signal in ecu.generators else ""
            lines.append(f"    signal {signal} = {value:g}{driven}")
        for pid, source in sorted(ecu.obd.bindings().items()):
            lines.append(f"    pid 01:{pid:02X} -> {source}")
        if ecu.dtcs.list():
            lines.append(f"    dtcs {' '.join(ecu.dtcs.list())}")
        for fault in vehicle.faults(ecu.name):
            lines.append(f"    fault {_fault_text(fault.kind, fault.until, vehicle)}")
    if vehicle.scenario is not None:
        lines.append(f"  scenario {len(vehicle.scenario.events)} events")
    return "\n".join(lines)


def _fault_text(kind: str, until: float, vehicle: Vehicle) -> str:
    if math.isinf(until):
        return kind
    return f"{kind} ({until - vehicle.clock.now():.3f}s left)"


class VehicleShell:
    """Intérprete de comandos sobre un ``Vehicle``; seguro entre hilos."""

    def __init__(self, vehicle: Vehicle) -> None:
        self.vehicle = vehicle
        self._commands: dict[str, Callable[[list[str]], str]] = {
            "help": lambda _: HELP,
            "list": self._list,
            "inspect": lambda _: describe(self.vehicle),
            "get": self._get,
            "set": self._set,
            "dtc": self._dtc,
            "fault": self._fault,
            "faults": self._faults,
            "pause": lambda _: self._lifecycle(self.vehicle.pause),
            "resume": lambda _: self._lifecycle(self.vehicle.resume),
            "reset": lambda _: self._reset(),
        }

    def execute(self, line: str) -> str:
        """Ejecuta una línea; los errores se devuelven como ``ERROR: ...``."""
        words = line.split()
        if not words:
            return ""
        command = self._commands.get(words[0].lower())
        if command is None:
            return f"ERROR: comando desconocido: {words[0]}"
        try:
            with self.vehicle.lock:
                self.vehicle.tick()
                return command(words[1:])
        except (ECUSDKError, ValueError, KeyError, IndexError) as error:
            message = error.args[0] if error.args else type(error).__name__
            return f"ERROR: {message}"

    def session(self) -> VehicleShell:
        """El shell no tiene estado por conexión; devuelve la misma instancia."""
        return self

    def serve(self, transport: ByteTransport) -> None:
        """Ejecuta cada línea recibida hasta EOF y responde una línea por comando."""
        serve_lines(transport, lambda line: self.execute(line) + "\n")

    def _list(self, args: list[str]) -> str:
        if args != ["ecus"]:
            raise ValueError("uso: list ecus")
        return "\n".join(ecu.name for ecu in self.vehicle.ecus)

    def _signal(self, target: str) -> tuple[str, str]:
        ecu, _, signal = target.partition(".")
        if not signal:
            raise ValueError("se esperaba ECU.signal")
        return self.vehicle.ecu(ecu).name, signal

    def _get(self, args: list[str]) -> str:
        (target,) = args
        ecu, signal = self._signal(target)
        return f"{self.vehicle.ecu(ecu).get_signal(signal):g}"

    def _set(self, args: list[str]) -> str:
        target, value = args
        ecu, signal = self._signal(target)
        self.vehicle.ecu(ecu).set_signal(signal, _number(value))
        return "OK"

    def _dtc(self, args: list[str]) -> str:
        ecu = self.vehicle.ecu(args[0])
        action = args[1].lower()
        if action == "list":
            return " ".join(ecu.dtcs.list()) or "(none)"
        if action == "clear":
            ecu.dtcs.clear()
        elif action in ("add", "remove"):
            getattr(ecu.dtcs, action)(args[2])
        else:
            raise ValueError(f"acción DTC desconocida: {action}")
        return "OK"

    def _fault(self, args: list[str]) -> str:
        ecu, kind, *rest = args
        if kind.lower() == "clear":
            self.vehicle.clear_fault(ecu, rest[0] if rest else None)
            return "OK"
        if kind not in FAULT_KINDS and kind != "offline":
            raise ValueError(f"fallo desconocido: {kind}")
        duration = math.inf
        if rest and "=" not in rest[0]:
            duration = parse_duration(rest.pop(0))
        options: dict[str, str] = dict(item.split("=", 1) for item in rest)
        unknown = set(options) - {
            "signal",
            "value",
            "delay",
            "probability",
            "sequence",
            "period",
        }
        if unknown:
            raise ValueError(f"opciones desconocidas: {' '.join(sorted(unknown))}")
        value = options.get("value")
        self.vehicle.inject_fault(
            kind,
            ecu,
            duration=duration,
            signal=options.get("signal"),
            value=_number(value) if value is not None else None,
            delay=parse_duration(options.get("delay", "0")),
            probability=float(options.get("probability", 1)),
            sequence=int(options.get("sequence", 1)),
            period=parse_duration(options.get("period", "1")),
        )
        return "OK"

    def _faults(self, args: list[str]) -> str:
        faults = self.vehicle.faults(args[0] if args else None)
        return (
            "\n".join(
                f"{fault.ecu} {_fault_text(fault.kind, fault.until, self.vehicle)}"
                for fault in faults
            )
            or "(none)"
        )

    def _lifecycle(self, action: Callable[[], None]) -> str:
        action()
        return self.vehicle.state.value

    def _reset(self) -> str:
        self.vehicle.reset()
        self.vehicle.start()
        return self.vehicle.state.value


def send_command(host: str, port: int, line: str, timeout: float = 5.0) -> str:
    """Envía un comando a un ``VehicleShell`` por TCP y devuelve la respuesta."""
    with socket.create_connection((host, port), timeout) as connection:
        connection.sendall(line.encode("utf-8") + b"\n")
        connection.shutdown(socket.SHUT_WR)
        data = bytearray()
        while chunk := connection.recv(4096):
            data.extend(chunk)
    return data.decode("utf-8").rstrip("\n")
