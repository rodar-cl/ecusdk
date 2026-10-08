"""Grabación y reproducción de sesiones en formato ``.eculog`` (versión 1).

Un ``.eculog`` es JSON Lines en UTF-8. La primera línea es la cabecera::

    {"format": "eculog", "version": 1, "vehicle": "demo-car", "seed": 0,
     "buses": ["powertrain"], "ecus": [{"name": "ecm", ...}]}

Cada línea siguiente es un registro con ``t`` (segundos desde el inicio de la
grabación, no decrecientes) y ``type``: ``state``, ``lifecycle``, ``frame``,
``diag``, ``signal``, ``dtc``, ``fault`` o ``scenario``. La especificación
completa está en ``docs/recording.md``.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import IO, Any, TypeAlias, cast

from ecusdk.can import CanFrame, VirtualCanBus, VirtualCanNode
from ecusdk.clock import Clock, RealClock, VirtualClock
from ecusdk.errors import AdapterError, IsoTpError, RecordingError
from ecusdk.faults import Fault
from ecusdk.isotp import IsoTpReceiver
from ecusdk.simulation import ScenarioEvent, Vehicle

FORMAT = "eculog"
VERSION = 1
RECORD_TYPES = frozenset(
    {"state", "lifecycle", "frame", "diag", "signal", "dtc", "fault", "scenario"}
)

Record: TypeAlias = dict[str, Any]


def _finite(value: float) -> float | None:
    return value if math.isfinite(value) else None


def _protocol(payload: bytes, direction: str) -> str:
    service = payload[1] if payload[0] == 0x7F and len(payload) > 1 else payload[0]
    if direction == "response" and payload[0] != 0x7F:
        service -= 0x40
    return "obd" if 0 <= service <= 0x0A else "uds"


class Recorder:
    """Graba un ``Vehicle`` en un ``.eculog`` mientras está abierto.

    Se engancha a ``vehicle.recorder`` y captura frames de los buses virtuales,
    peticiones y respuestas OBD/UDS reensambladas, cambios de señales
    (valores observables), DTCs, fallos, eventos de escenario y lifecycle.
    Los cambios hechos entre ticks se registran en el siguiente ``tick``.
    Los buses no virtuales (SocketCAN) no se graban a nivel de frame.
    """

    def __init__(self, vehicle: Vehicle, target: str | Path | IO[str]) -> None:
        if vehicle.recorder is not None:
            raise RecordingError("el vehículo ya tiene un recorder activo")
        self.vehicle = vehicle
        if isinstance(target, (str, Path)):
            self._file: IO[str] = Path(target).open("w", encoding="utf-8", newline="\n")
            self._owns_file = True
        else:
            self._file, self._owns_file = target, False
        self._start = vehicle.clock.now()
        self._closed = False
        self._pending: list[Record] = []
        self._taps: dict[str, VirtualCanNode] = {
            name: bus.connect()
            for name, bus in vehicle.buses.items()
            if isinstance(bus, VirtualCanBus)
        }
        self._routes: dict[tuple[str, int, bool], tuple[str, str]] = {}
        for ecu in vehicle.ecus:
            for bus in vehicle.buses:
                if ecu.bus is None or ecu.bus == bus:
                    self._routes[(bus, ecu.request_id, False)] = (ecu.name, "request")
                    self._routes[(bus, ecu.response_id, False)] = (ecu.name, "response")
        self._receivers: dict[tuple[str, int, bool], IsoTpReceiver] = {}
        self._signals: dict[str, dict[str, float]] = {}
        self._dtcs: dict[str, set[str]] = {}
        self._faults: set[Fault] = set()
        self._state = vehicle.state
        self._write_line(
            {
                "format": FORMAT,
                "version": VERSION,
                "vehicle": vehicle.name,
                "seed": vehicle.seed,
                "buses": list(vehicle.buses),
                "ecus": [
                    {
                        "name": ecu.name,
                        "request_id": ecu.request_id,
                        "response_id": ecu.response_id,
                        "bus": ecu.bus,
                    }
                    for ecu in vehicle.ecus
                ],
            }
        )
        with vehicle.lock:
            self._write_line(
                {"t": 0.0, "type": "lifecycle", "state": self._state.value}
            )
            for ecu in vehicle.ecus:
                self._signals[ecu.name] = dict(ecu.signals)
                self._dtcs[ecu.name] = set(ecu.dtcs.list())
                self._write_line(
                    {
                        "t": 0.0,
                        "type": "state",
                        "ecu": ecu.name,
                        "signals": self._signals[ecu.name],
                        "dtcs": sorted(self._dtcs[ecu.name]),
                    }
                )
            vehicle.recorder = self

    def __enter__(self) -> Recorder:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _t(self, at: float | None = None) -> float:
        now = self.vehicle.clock.now() if at is None else at
        return round(max(now - self._start, 0.0), 9)

    def _write_line(self, record: Record) -> None:
        self._file.write(json.dumps(record, separators=(",", ":")) + "\n")

    def scenario_event(self, event: ScenarioEvent) -> None:
        """Registra un evento de escenario (lo llama ``Vehicle.tick``)."""
        record = {"t": self._t(), "type": "scenario"} | asdict(event)
        self._pending.append(record)

    def capture(self) -> None:
        """Registra frames y cambios de estado desde la captura anterior."""
        if self._closed:
            return
        records = self._pending
        self._pending = []
        for bus, tap in list(self._taps.items()):
            try:
                while (frame := tap.recv(timeout=0)) is not None:
                    records.extend(self._frame_records(bus, frame))
            except AdapterError:
                del self._taps[bus]  # el bus se cerró o se reinició
        now = self._t()
        vehicle = self.vehicle
        if vehicle.state is not self._state:
            self._state = vehicle.state
            records.append({"t": now, "type": "lifecycle", "state": self._state.value})
        for ecu in vehicle.ecus:
            known = self._signals[ecu.name]
            for name, value in ecu.signals.items():
                if known.get(name) != value:
                    known[name] = value
                    records.append(
                        {
                            "t": now,
                            "type": "signal",
                            "ecu": ecu.name,
                            "signal": name,
                            "value": value,
                        }
                    )
            codes = set(ecu.dtcs.list())
            previous = self._dtcs[ecu.name]
            for action, changed in (
                ("add", codes - previous),
                ("remove", previous - codes),
            ):
                records.extend(
                    {
                        "t": now,
                        "type": "dtc",
                        "ecu": ecu.name,
                        "action": action,
                        "code": code,
                    }
                    for code in sorted(changed)
                )
            self._dtcs[ecu.name] = codes
        faults = set(vehicle.faults())
        for action, changed in (
            ("end", self._faults - faults),
            ("start", faults - self._faults),
        ):
            for fault in changed:
                # Un fallo que expiró solo termina en su ``until``, no al capturar.
                at = min(now, self._t(fault.until)) if action == "end" else now
                records.append(self._fault_record(at, action, fault))
        self._faults = faults
        records.sort(key=lambda record: record["t"])
        for record in records:
            self._write_line(record)
        self._file.flush()

    def _fault_record(self, now: float, action: str, fault: Fault) -> Record:
        record = {"t": now, "type": "fault", "action": action} | asdict(fault)
        record["start"] = self._t(fault.start)
        record["until"] = _finite(fault.until)
        if record["until"] is not None:
            record["until"] = self._t(fault.until)
        return record

    def _frame_records(self, bus: str, frame: CanFrame) -> list[Record]:
        t = self._t(frame.timestamp)
        records: list[Record] = [
            {
                "t": t,
                "type": "frame",
                "bus": bus,
                "id": frame.arbitration_id,
                "ext": frame.is_extended_id,
                "data": frame.data.hex(),
            }
        ]
        key = (bus, frame.arbitration_id, frame.is_extended_id)
        route = self._routes.get(key)
        if route is None or not frame.data or frame.data[0] >> 4 == 3:
            return records
        receiver = self._receivers.setdefault(key, IsoTpReceiver(clock=VirtualClock()))
        try:
            payload = receiver.push(frame)
        except IsoTpError:
            return records
        if payload:
            ecu, direction = route
            records.append(
                {
                    "t": t,
                    "type": "diag",
                    "ecu": ecu,
                    "direction": direction,
                    "protocol": _protocol(payload, direction),
                    "data": payload.hex(),
                }
            )
        return records

    def close(self) -> None:
        """Captura lo pendiente, se desengancha del vehículo y cierra el archivo."""
        if self._closed:
            return
        with self.vehicle.lock:
            self.capture()
            self._closed = True
            if self.vehicle.recorder is self:
                self.vehicle.recorder = None
            for tap in self._taps.values():
                tap.close()
        if self._owns_file:
            self._file.close()


@dataclass(frozen=True, slots=True)
class EcuLog:
    """Contenido validado de un ``.eculog``."""

    header: Record
    records: list[Record]

    def frames(self, bus: str | None = None) -> Iterator[tuple[float, str, CanFrame]]:
        """``(t, bus, frame)`` de los registros ``frame``, opcionalmente de un bus."""
        for record in self.records:
            if record["type"] == "frame" and bus in (None, record["bus"]):
                yield record["t"], record["bus"], frame_of(record)

    def state_at(self, t: float) -> dict[str, Record]:
        """Estado reconstruido en ``t``: señales, DTCs y fallos activos por ECU."""
        state: dict[str, Record] = {}
        for record in self.records:
            if record["t"] > t:
                break
            kind = record["type"]
            if kind == "state":
                state[record["ecu"]] = {
                    "signals": dict(record["signals"]),
                    "dtcs": set(record["dtcs"]),
                    "faults": set[str](),
                }
            elif kind == "signal":
                state[record["ecu"]]["signals"][record["signal"]] = record["value"]
            elif kind == "dtc":
                codes = state[record["ecu"]]["dtcs"]
                (codes.add if record["action"] == "add" else codes.discard)(
                    record["code"]
                )
            elif kind == "fault":
                faults = state[record["ecu"]]["faults"]
                (faults.add if record["action"] == "start" else faults.discard)(
                    record["kind"]
                )
        return {
            ecu: {
                "signals": values["signals"],
                "dtcs": sorted(values["dtcs"]),
                "faults": sorted(values["faults"]),
            }
            for ecu, values in state.items()
        }


def frame_of(record: Record) -> CanFrame:
    """Convierte un registro ``frame`` en ``CanFrame`` con su ``t`` como timestamp."""
    return CanFrame(
        record["id"], bytes.fromhex(record["data"]), record["ext"], record["t"]
    )


def read_log(path: str | Path) -> EcuLog:
    """Lee y valida un ``.eculog``; RecordingError si es inválido."""
    with Path(path).open(encoding="utf-8") as file:
        lines = [line for line in file if line.strip()]
    if not lines:
        raise RecordingError("eculog vacío")
    parsed: list[Record] = []
    for number, line in enumerate(lines, start=1):
        try:
            value: object = json.loads(line)
        except json.JSONDecodeError as error:
            raise RecordingError(f"JSON inválido en la línea {number}") from error
        if not isinstance(value, dict):
            raise RecordingError(f"registro inválido en la línea {number}")
        parsed.append(cast(Record, value))
    header, records = parsed[0], parsed[1:]
    if header.get("format") != FORMAT:
        raise RecordingError("no es un archivo eculog")
    if header.get("version") != VERSION:
        raise RecordingError(f"versión eculog no soportada: {header.get('version')!r}")
    previous = 0.0
    for number, record in enumerate(records, start=2):
        t: object = record.get("t")
        if record.get("type") not in RECORD_TYPES or type(t) not in (int, float):
            raise RecordingError(f"registro inválido en la línea {number}")
        if cast(float, t) < previous:
            raise RecordingError(f"timestamp decreciente en la línea {number}")
        previous = cast(float, t)
    return EcuLog(header, records)


def replay(
    log: EcuLog,
    handler: Callable[[Record], None],
    *,
    clock: Clock | None = None,
    speed: float = 1.0,
) -> None:
    """Entrega cada registro a ``handler`` respetando el orden y los tiempos.

    ``speed`` multiplica la velocidad (2 = el doble de rápido); ``0`` entrega
    todo sin esperar. Con ``VirtualClock`` la espera es instantánea.
    """
    if not math.isfinite(speed) or speed < 0:
        raise ValueError("speed debe ser finito y no negativo")
    clock = clock or RealClock()
    start = clock.now()
    for record in log.records:
        if speed:
            wait = start + record["t"] / speed - clock.now()
            if wait > 0:
                clock.sleep(wait)
        handler(record)


def format_record(record: Record) -> str:
    """Línea legible de un registro, estilo candump."""
    t, kind = record["t"], record["type"]
    if kind == "frame":
        data = bytes.fromhex(record["data"])
        identifier = f"{record['id']:08X}" if record["ext"] else f"{record['id']:03X}"
        detail = f"{record['bus']} {identifier} [{len(data)}] {data.hex(' ').upper()}"
    elif kind == "diag":
        data = bytes.fromhex(record["data"]).hex(" ").upper()
        arrow = "->" if record["direction"] == "request" else "<-"
        detail = f"{record['ecu']} {arrow} {record['protocol']} {data}"
    elif kind == "signal":
        detail = f"{record['ecu']}.{record['signal']} = {record['value']:g}"
    elif kind == "dtc":
        detail = f"{record['ecu']} {record['action']} {record['code']}"
    elif kind == "fault":
        detail = f"{record['ecu']} {record['action']} {record['kind']}"
    elif kind == "scenario":
        detail = f"{record['ecu']} {record['action']}"
    elif kind == "lifecycle":
        detail = record["state"]
    else:
        detail = f"{record['ecu']} {len(record['signals'])} signals"
    return f"{t:12.6f} {kind:<9} {detail}"
