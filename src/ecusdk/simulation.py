"""Runtime de vehículos, ECUs, señales, escenarios y fallos deterministas."""

from __future__ import annotations

import heapq
import itertools
import json
import math
import random
import threading
from collections.abc import Callable, Iterator, MutableMapping, Sequence
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Protocol, cast

from ecusdk.can import CanBus, CanFilter, CanFrame, VirtualCanBus, VirtualCanNode
from ecusdk.clock import Clock, RealClock
from ecusdk.errors import (
    IsoTpError,
    ObdError,
    SimulationError,
    VehicleStateError,
)
from ecusdk.faults import (
    DELAY_FAULTS,
    FAULT_ALIASES,
    FAULT_KINDS,
    FRAME_FAULTS,
    SENSOR_FAULTS,
    Fault,
)
from ecusdk.isotp import reassemble
from ecusdk.isotp_transport import IsoTpTransport
from ecusdk.obd import DtcStore, ObdRegistry, encode_dtc
from ecusdk.signals import SignalGenerator, SignalValue
from ecusdk.uds import UdsServer

__all__ = [
    "ECU",
    "Scenario",
    "ScenarioEvent",
    "SignalMap",
    "SignalValue",
    "Vehicle",
    "VehicleObserver",
    "VehicleState",
]


class VehicleState(Enum):
    """Estado del lifecycle de un ``Vehicle``."""

    STOPPED = "stopped"
    RUNNING = "running"
    PAUSED = "paused"


class SignalMap(MutableMapping[str, SignalValue]):
    """Dictionary-compatible signal store with range and finite-value checks."""

    def __init__(
        self,
        values: dict[str, SignalValue],
        limits: dict[str, tuple[float | None, float | None]] | None = None,
    ) -> None:
        self._values = dict(values)
        self._limits = limits or {}
        self._overrides: dict[str, SignalValue] = {}
        for name, value in self._values.items():
            self._validate(name, value)

    def _validate(self, name: str, value: SignalValue) -> None:
        if name not in self._values and name not in self._limits:
            raise KeyError(f"señal desconocida: {name}")
        if type(value) not in (int, float) or not math.isfinite(float(value)):
            raise ValueError(f"valor inválido para señal {name}")
        minimum, maximum = self._limits.get(name, (None, None))
        if (
            minimum is not None
            and value < minimum
            or maximum is not None
            and value > maximum
        ):
            raise ValueError(f"valor fuera de rango para señal {name}")

    def __getitem__(self, key: str) -> SignalValue:
        if key in self._overrides:
            return self._overrides[key]
        return self._values[key]

    def override(self, name: str, value: SignalValue) -> None:
        """Fija el valor observable sin límites de rango (fallos de sensor).

        Las escrituras siguen actualizando el valor real, que vuelve a verse
        al llamar ``clear_overrides``.
        """
        if name not in self._values:
            raise KeyError(f"señal desconocida: {name}")
        if type(value) not in (int, float) or not math.isfinite(float(value)):
            raise ValueError(f"valor inválido para señal {name}")
        self._overrides[name] = value

    def clear_overrides(self) -> None:
        self._overrides.clear()

    def __setitem__(self, key: str, value: SignalValue) -> None:
        self._validate(key, value)
        self._values[key] = value

    def __delitem__(self, key: str) -> None:
        raise TypeError("las señales no se pueden eliminar")

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)


@dataclass(slots=True)
class ECU:
    name: str
    request_id: int
    response_id: int
    signals: MutableMapping[str, SignalValue] = field(
        default_factory=lambda: dict[str, SignalValue]()
    )
    vin: str = ""
    dtcs: DtcStore = field(default_factory=DtcStore)
    online: bool = True
    bus: str | None = None
    signal_limits: dict[str, tuple[float | None, float | None]] = field(
        default_factory=lambda: dict[str, tuple[float | None, float | None]]()
    )
    generators: dict[str, SignalGenerator] = field(
        default_factory=lambda: dict[str, SignalGenerator]()
    )
    _initial_signals: dict[str, SignalValue] = field(init=False, repr=False)
    _transport: IsoTpTransport = field(init=False, repr=False)
    _clock: Clock = field(init=False, repr=False)
    obd: ObdRegistry = field(init=False, repr=False)
    uds: UdsServer = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.signals = SignalMap(dict(self.signals), self.signal_limits)
        self._initial_signals = dict(self.signals)
        self._transport = IsoTpTransport(self.response_id, self.request_id)
        self.obd = ObdRegistry(self.signals)
        self._clock = RealClock()
        self.uds = UdsServer(self.signals, lambda: self.dtcs.list(), self._uds_reset)
        if len(self.vin) == 17:
            self.uds.did(0xF190, self.vin)
        generators, self.generators = self.generators, {}
        for name, generator in generators.items():
            self.drive(name, generator)

    @property
    def signal_map(self) -> SignalMap:
        """Las señales como ``SignalMap``, con acceso a overrides de fallos."""
        return cast(SignalMap, self.signals)

    def drive(self, name: str, generator: SignalGenerator | None) -> None:
        """Asocia un generador a una señal, o lo quita con ``None``.

        Cada ``Vehicle.tick`` escribe ``generator(elapsed)`` acotado a los
        límites de la señal. Cualquier callable sirve como generador custom.
        """
        if name not in self.signals:
            raise KeyError(f"señal desconocida: {name}")
        if generator is None:
            self.generators.pop(name, None)
        elif not callable(generator):
            raise SimulationError("el generador debe ser callable")
        else:
            self.generators[name] = generator

    def reset(self) -> None:
        """Cancela ISO-TP, vuelve a la sesión UDS por defecto y restaura señales."""
        self._transport.reset()
        self.uds.reset()
        for name, value in self._initial_signals.items():
            self.signals[name] = value

    def set_protocol_clock(self, clock: Clock) -> None:
        """Asigna el reloj de transporte y cancela transferencias pendientes."""
        self._clock = clock
        self._transport = IsoTpTransport(self.response_id, self.request_id, clock=clock)

    def get_signal(self, name: str) -> SignalValue:
        """Valor observable de una señal; KeyError si no existe."""
        try:
            return self.signals[name]
        except KeyError as error:
            raise KeyError(f"señal desconocida: {name}") from error

    def set_signal(self, name: str, value: SignalValue) -> None:
        """Escribe una señal validando tipo, finitud y límites."""
        self.signals[name] = value

    def _uds_reset(self, kind: int) -> None:
        # hardReset y keyOffOnReset reinician el estado; softReset sólo la sesión.
        if kind in (0x01, 0x02):
            for name, value in self._initial_signals.items():
                self.signals[name] = value

    def diagnostic_payload(self, request: bytes) -> bytes | None:
        """Respuesta OBD (modos 0x01–0x0A) o UDS (resto de SIDs), o ``None``."""
        if not request:
            return None
        if request[0] <= 0x0A:
            return self.obd_payload(request)
        return self.uds.handle(request, self._clock.now())

    def obd_payload(self, request: bytes) -> bytes | None:
        """Respuesta OBD a un payload de petición, o ``None`` si no se soporta."""
        if not request:
            return None
        mode = request[0]
        pid = request[1] if len(request) > 1 else 0
        if mode == 0x01:
            return self.obd.encode(pid)
        if mode == 0x03:
            return bytes((0x43,)) + b"".join(
                encode_dtc(code) for code in self.dtcs.list()
            )
        if mode == 0x04:
            self.dtcs.clear()
            return bytes((0x44,))
        if mode == 0x09 and pid == 0x02:
            if len(self.vin) != 17:
                raise ValueError("el VIN debe contener 17 caracteres")
            return bytes((0x49, 0x02, 0x01)) + self.vin.encode("ascii")
        return None

    def handle_frames(self, frame: CanFrame) -> list[CanFrame] | None:
        """Procesa un frame dirigido a la ECU y devuelve los frames a emitir."""
        if not self.online or frame.arbitration_id != self.request_id:
            return None
        try:
            payload = self._transport.receive(frame)
            if payload is not None:
                response_payload = self.diagnostic_payload(payload)
                if response_payload is not None:
                    self._transport.send(response_payload)
            return self._transport.poll()
        except (IsoTpError, ObdError, ValueError, IndexError):
            self._transport.reset()
            return None

    def poll_frames(self) -> list[CanFrame]:
        """Obtiene envíos ISO-TP habilitados por el reloj, sin nuevos inputs.

        No emite mientras la ECU está offline. Los errores de protocolo o
        timeout cancelan la transferencia y se traducen en ausencia de frames,
        igual que en handle_frames.
        """
        if not self.online:
            return []
        try:
            return self._transport.poll()
        except IsoTpError:
            self._transport.reset()
            return []

    def handle(self, frame: CanFrame) -> CanFrame | None:
        """Como ``handle_frames`` pero sólo el primer frame de respuesta."""
        frames = self.handle_frames(frame)
        return frames[0] if frames else None


_SIGNAL_ACTIONS = frozenset({"set", "signal"})
_EVENT_ACTIONS = _SIGNAL_ACTIONS | FAULT_KINDS | {"clear_fault"} | set(FAULT_ALIASES)
_FAULT_OPTIONS = frozenset({"delay", "probability", "sequence", "period"})


@dataclass(frozen=True, slots=True)
class ScenarioEvent:
    """Acción programada ``at`` segundos después de iniciar el escenario.

    ``action`` es ``set``, ``clear_fault`` o un tipo de fallo (ver
    ``ecusdk.faults.FAULT_KINDS``). En fallos, ``duration`` 0 significa hasta
    ``clear_fault`` y ``options`` lleva delay, probability, sequence o period.
    """

    at: float
    ecu: str
    action: str
    signal: str | None = None
    value: SignalValue | None = None
    duration: float = 0.0
    options: dict[str, float] = field(
        default_factory=lambda: dict[str, float](), hash=False
    )

    def __post_init__(self) -> None:
        if not math.isfinite(self.at) or self.at < 0:
            raise SimulationError("at debe ser finito y no negativo")
        if self.action not in _EVENT_ACTIONS:
            raise SimulationError(f"acción de escenario desconocida: {self.action}")
        if not math.isfinite(self.duration) or self.duration < 0:
            raise SimulationError("duration debe ser finito y no negativo")
        unknown = set(self.options) - _FAULT_OPTIONS
        if unknown:
            raise SimulationError(f"opciones de fallo desconocidas: {sorted(unknown)}")


class Scenario:
    """Ordered, relative-time events applied by :meth:`Vehicle.tick`."""

    def __init__(self, events: Sequence[ScenarioEvent | dict[str, Any]] = ()) -> None:
        self.events = sorted(
            (self._event(event) for event in events), key=lambda event: event.at
        )
        self._cursor = 0
        self._origin: float | None = None
        self._paused_at: float | None = None

    @staticmethod
    def _event(event: ScenarioEvent | dict[str, Any]) -> ScenarioEvent:
        if isinstance(event, ScenarioEvent):
            return event
        known = {"at", "time", "ecu", "action", "signal", "value", "duration"}
        options = {
            str(key): float(value)
            for key, value in event.items()
            if key not in known and key != "options"
        }
        options.update(event.get("options", {}))
        try:
            return ScenarioEvent(
                at=float(event.get("at", event.get("time", 0))),
                ecu=str(event["ecu"]),
                action=str(event["action"]),
                signal=event.get("signal"),
                value=event.get("value"),
                duration=float(event.get("duration", 0)),
                options=options,
            )
        except KeyError as error:
            raise SimulationError(f"evento sin campo {error}") from error

    @property
    def started(self) -> bool:
        """Indica si el escenario ya tiene origen de tiempo."""
        return self._origin is not None

    def start(self, now: float) -> None:
        """Fija el origen de tiempo en ``now``."""
        self._origin = now

    def reset(self) -> None:
        """Vuelve al primer evento y olvida origen y pausa."""
        self._cursor = 0
        self._origin = None
        self._paused_at = None

    def pause(self, now: float) -> None:
        """Congela el tiempo del escenario en ``now``."""
        self._paused_at = now

    def resume(self, now: float) -> None:
        """Reanuda desplazando el origen lo que duró la pausa."""
        if self._paused_at is not None and self._origin is not None:
            self._origin += now - self._paused_at
        self._paused_at = None

    def elapsed(self, now: float) -> float:
        """Segundos de escenario transcurridos, sin contar pausas."""
        if self._origin is None:
            self.start(now)
        origin = self._origin
        assert origin is not None
        end = self._paused_at if self._paused_at is not None else now
        return _elapsed(end - origin)

    def due(self, now: float) -> list[ScenarioEvent]:
        """Eventos vencidos desde la última llamada, en orden."""
        elapsed = self.elapsed(now)
        due: list[ScenarioEvent] = []
        while (
            self._cursor < len(self.events) and self.events[self._cursor].at <= elapsed
        ):
            due.append(self.events[self._cursor])
            self._cursor += 1
        return due

    def to_json(self) -> str:
        """Serializa los eventos a JSON."""
        return json.dumps([asdict(event) for event in self.events], sort_keys=True)

    @classmethod
    def from_json(cls, value: str) -> Scenario:
        """Crea un escenario desde ``to_json``."""
        return cls(json.loads(value))


def _elapsed(seconds: float) -> float:
    # Redondear a ns hace que repetir sobre otro instante absoluto del reloj
    # no altere qué eventos vencen por error de coma flotante.
    return round(seconds, 9)


def _clamp(value: SignalValue, limits: tuple[float | None, float | None]) -> float:
    minimum, maximum = limits
    if minimum is not None:
        value = max(value, minimum)
    if maximum is not None:
        value = min(value, maximum)
    return value


class VehicleObserver(Protocol):
    """Observador de un ``Vehicle``, p. ej. ``ecusdk.recording.Recorder``.

    ``capture`` se llama tras cada ``tick`` y cada cambio de lifecycle;
    ``scenario_event`` antes de aplicar cada evento de escenario.
    """

    def capture(self) -> None: ...

    def scenario_event(self, event: ScenarioEvent) -> None: ...


class Vehicle:
    """Vehículo virtual: buses, ECUs, reloj, escenario, fallos y lifecycle.

    ``seed`` inicializa la aleatoriedad de los fallos (``frame_drop``); con el
    mismo reloj, seed, escenario e inputs, una ejecución se repite exactamente.
    """

    def __init__(
        self,
        name: str,
        buses: dict[str, CanBus],
        ecus: list[ECU],
        clock: Clock | None = None,
        scenario: Scenario | None = None,
        *,
        seed: int = 0,
    ) -> None:
        self.name, self.buses, self.ecus = name, buses, ecus
        self.clock = clock or RealClock()
        self.state = VehicleState.STOPPED
        for ecu in self.ecus:
            ecu.set_protocol_clock(self.clock)
        self.scenario = scenario
        self.recorder: VehicleObserver | None = None
        self.seed = seed
        self._rng = random.Random(seed)
        self._faults: list[Fault] = []
        self._delayed: list[
            tuple[float, int, Callable[[CanFrame], None], CanFrame]
        ] = []
        self._order = itertools.count()
        self._started_at: float | None = None
        self._paused_at: float | None = None
        self._paused_total = 0.0
        self._exchange_lock = threading.RLock()
        self._virtual_nodes: dict[str, list[tuple[ECU, VirtualCanNode]]] = {}
        self._protocol_buses: dict[int, str] = {}
        for bus in self.buses.values():
            if isinstance(bus, VirtualCanBus):
                bus.set_clock(self.clock)

    @property
    def lock(self) -> threading.RLock:
        """Lock reentrante que serializa transacciones y cambios de estado."""
        return self._exchange_lock

    def ecu(self, name: str) -> ECU:
        """Busca una ECU por nombre, sin distinguir mayúsculas."""
        for ecu in self.ecus:
            if ecu.name == name:
                return ecu
        for ecu in self.ecus:
            if ecu.name.lower() == name.lower():
                return ecu
        raise KeyError(f"ECU desconocida: {name}")

    def elapsed(self) -> float:
        """Segundos en ejecución desde ``start``, sin contar pausas."""
        if self._started_at is None:
            return 0.0
        end = self._paused_at if self._paused_at is not None else self.clock.now()
        return _elapsed(end - self._started_at - self._paused_total)

    def _connect_virtual_nodes(self) -> None:
        try:
            for name, bus in self.buses.items():
                if not isinstance(bus, VirtualCanBus) or name in self._virtual_nodes:
                    continue
                ports: list[tuple[ECU, VirtualCanNode]] = []
                self._virtual_nodes[name] = ports
                for ecu in self.ecus:
                    if ecu.bus is None or ecu.bus == name:
                        port = bus.connect(
                            filters=[CanFilter(ecu.request_id, 0x7FF, False)]
                        )
                        ports.append((ecu, port))
        except Exception:
            self._disconnect_virtual_nodes()
            raise

    def _disconnect_virtual_nodes(self) -> None:
        for ports in self._virtual_nodes.values():
            for _, port in ports:
                port.close()
        self._virtual_nodes.clear()
        self._protocol_buses.clear()
        self._delayed.clear()

    def start(self) -> None:
        """Conecta las ECUs a sus buses virtuales y pasa a RUNNING."""
        with self._exchange_lock:
            self._connect_virtual_nodes()
            self.state = VehicleState.RUNNING
            if self._started_at is None:
                self._started_at = self.clock.now()
            if self.scenario is not None and not self.scenario.started:
                self.scenario.start(self.clock.now())
            self._observe()

    def stop(self) -> None:
        """Desconecta las ECUs y pasa a STOPPED; conserva señales y fallos."""
        with self._exchange_lock:
            self.state = VehicleState.STOPPED
            self._disconnect_virtual_nodes()
            for ecu in self.ecus:
                ecu.set_protocol_clock(self.clock)
            self._observe()

    def pause(self) -> None:
        """Congela escenario, generadores y tráfico hasta ``resume``."""
        with self._exchange_lock:
            if self.state is VehicleState.RUNNING:
                now = self.clock.now()
                if self.scenario is not None:
                    self.scenario.pause(now)
                self._paused_at = now
                self.state = VehicleState.PAUSED
                self._observe()

    def resume(self) -> None:
        """Reanuda desde PAUSED sin contar el tiempo pausado."""
        with self._exchange_lock:
            if self.state is VehicleState.PAUSED:
                now = self.clock.now()
                if self.scenario is not None:
                    self.scenario.resume(now)
                if self._paused_at is not None:
                    self._paused_total += now - self._paused_at
                self._paused_at = None
                self.state = VehicleState.RUNNING
                self._observe()

    def reset(self) -> None:
        """Detiene y vuelve al estado inicial: señales, fallos, seed y escenario."""
        with self._exchange_lock:
            self.stop()
            self._faults.clear()
            self._rng = random.Random(self.seed)
            self._started_at = self._paused_at = None
            self._paused_total = 0.0
            for ecu in self.ecus:
                ecu.online = True
                ecu.signal_map.clear_overrides()
                ecu.reset()
            if self.scenario is not None:
                self.scenario.reset()
            self._observe()

    def tick(self) -> None:
        """Avanza escenario, señales y fallos; procesa un frame por nodo ECU."""
        with self._exchange_lock:
            if self.state is not VehicleState.RUNNING:
                return
            if self.scenario is not None:
                now = self.clock.now()
                elapsed = self.scenario.elapsed(now)
                for event in self.scenario.due(now):
                    if self.recorder is not None:
                        self.recorder.scenario_event(event)
                    self._apply_event(event, now - (elapsed - event.at))
            self._update_signals()
            self._service_virtual_buses()
            self._observe()

    def _observe(self) -> None:
        if self.recorder is not None:
            self.recorder.capture()

    def advance(self, seconds: float, step: float = 0.01) -> None:
        """Duerme el reloj en pasos de ``step`` llamando a ``tick`` en cada uno.

        Con ``VirtualClock`` es instantáneo y determinista.
        """
        if not math.isfinite(seconds) or seconds < 0 or step <= 0:
            raise SimulationError("seconds debe ser no negativo y step positivo")
        deadline = self.clock.now() + seconds
        self.tick()
        while (remaining := deadline - self.clock.now()) > 0:
            self.clock.sleep(min(step, remaining))
            self.tick()

    def _apply_event(self, event: ScenarioEvent, at: float) -> None:
        ecu = self.ecu(event.ecu)
        if event.action in _SIGNAL_ACTIONS:
            if event.signal is None or event.value is None:
                raise SimulationError("evento de señal incompleto")
            ecu.set_signal(event.signal, event.value)
        elif event.action == "clear_fault":
            self.clear_fault(ecu.name)
        else:
            options = event.options
            self.inject_fault(
                event.action,
                ecu.name,
                duration=event.duration or math.inf,
                start=at,
                signal=event.signal,
                value=event.value,
                delay=options.get("delay", 0.0),
                probability=options.get("probability", 1.0),
                sequence=int(options.get("sequence", 1)),
                period=options.get("period", 1.0),
            )

    def inject_fault(
        self,
        kind: str,
        ecu: str,
        *,
        duration: float = math.inf,
        start: float | None = None,
        signal: str | None = None,
        value: SignalValue | None = None,
        delay: float = 0.0,
        probability: float = 1.0,
        sequence: int = 1,
        period: float = 1.0,
    ) -> Fault:
        """Activa un fallo durante ``duration`` segundos (indefinido por defecto).

        ``sensor_frozen`` sin ``value`` congela el valor actual de la señal.
        Devuelve el ``Fault`` registrado; ``clear_fault`` lo retira antes.
        """
        with self._exchange_lock:
            target = self.ecu(ecu)
            begin = self.clock.now() if start is None else start
            if not duration >= 0:
                raise SimulationError("duration debe ser no negativa")
            if signal is not None:
                target.get_signal(signal)
            if FAULT_ALIASES.get(kind, kind) == "sensor_frozen" and value is None:
                if signal is None:
                    raise SimulationError("sensor_frozen requiere signal")
                value = target.get_signal(signal)
            fault = Fault(
                kind,
                target.name,
                begin,
                begin + duration,
                signal,
                value,
                delay,
                probability,
                sequence,
                period,
            )
            self._faults.append(fault)
            self._update_signals()
            self._observe()
            return fault

    def clear_fault(self, ecu: str, kind: str | None = None) -> None:
        """Retira los fallos de una ECU, o sólo los de ``kind``."""
        with self._exchange_lock:
            name = self.ecu(ecu).name
            kind = FAULT_ALIASES.get(kind, kind) if kind is not None else None
            self._faults = [
                fault
                for fault in self._faults
                if fault.ecu != name or (kind is not None and fault.kind != kind)
            ]
            self._update_signals()
            self._observe()

    def faults(self, ecu: str | None = None) -> list[Fault]:
        """Fallos activos ahora, opcionalmente de una sola ECU."""
        now = self.clock.now()
        name = self.ecu(ecu).name if ecu is not None else None
        return [
            fault
            for fault in self._faults
            if fault.active(now) and (name is None or fault.ecu == name)
        ]

    def inject_timeout(self, ecu: str, duration: float) -> None:
        """La ECU recibe pero no responde durante ``duration`` segundos."""
        self.inject_fault("timeout", ecu, duration=duration)

    def set_ecu_offline(self, ecu: str, duration: float) -> None:
        """La ECU desaparece del bus durante ``duration`` segundos."""
        self.inject_fault("ecu_offline", ecu, duration=duration)

    def _active(self, ecu: ECU, kinds: frozenset[str]) -> list[Fault]:
        now = self.clock.now()
        return [
            fault
            for fault in self._faults
            if fault.ecu == ecu.name and fault.kind in kinds and fault.active(now)
        ]

    def _update_signals(self) -> None:
        now = self.clock.now()
        self._faults = [fault for fault in self._faults if now < fault.until]
        elapsed = self.elapsed()
        for ecu in self.ecus:
            signals = ecu.signal_map
            signals.clear_overrides()
            for name, generator in ecu.generators.items():
                limits = ecu.signal_limits.get(name, (None, None))
                signals[name] = _clamp(generator(elapsed), limits)
            for fault in self._active(ecu, SENSOR_FAULTS):
                assert fault.signal is not None
                if fault.kind != "intermittent_signal":
                    assert fault.value is not None
                    signals.override(fault.signal, fault.value)
                elif math.floor((now - fault.start) / fault.period) % 2:
                    signals.override(fault.signal, fault.value or 0)

    def _silent(self, ecu: ECU) -> bool:
        return bool(self._active(ecu, frozenset({"timeout"})))

    def _can_exchange(self, ecu: ECU, bus_name: str) -> bool:
        return (ecu.bus is None or ecu.bus == bus_name) and not self._active(
            ecu, frozenset({"ecu_offline"})
        )

    def _emit(
        self, ecu: ECU, frames: list[CanFrame], send: Callable[[CanFrame], None]
    ) -> None:
        """Envía frames de una ECU aplicando sus fallos de frame y retardo."""
        for fault in self._active(ecu, FRAME_FAULTS):
            frames = [out for frame in frames for out in fault.apply(frame, self._rng)]
        delays = self._active(ecu, DELAY_FAULTS)
        now = self.clock.now()
        for frame in frames:
            delay = max((fault.frame_delay(frame) for fault in delays), default=0.0)
            if delay > 0:
                item = (now + delay, next(self._order), send, frame)
                heapq.heappush(self._delayed, item)
            else:
                send(frame)

    def _release_delayed(self) -> None:
        now = self.clock.now()
        while self._delayed and self._delayed[0][0] <= now:
            _, _, send, frame = heapq.heappop(self._delayed)
            send(frame)

    def _service_virtual_buses(self) -> None:
        self._release_delayed()
        for bus_name, ports in self._virtual_nodes.items():
            for ecu, port in ports:
                frame = port.recv(timeout=0)
                if not self._can_exchange(ecu, bus_name) or self._silent(ecu):
                    continue
                if frame is not None:
                    self._protocol_buses[id(ecu)] = bus_name
                    self._emit(ecu, ecu.handle_frames(frame) or [], port.send)
                if self._protocol_buses.get(id(ecu), bus_name) == bus_name:
                    self._emit(ecu, ecu.poll_frames(), port.send)

    def exchange(
        self, bus_name: str, request: CanFrame, timeout: float = 0.0
    ) -> CanFrame | None:
        """Como ``exchange_frames`` pero sólo el primer frame de respuesta."""
        responses = self.exchange_frames(bus_name, request, timeout)
        return responses[0] if responses else None

    def exchange_frames(
        self, bus_name: str, request: CanFrame, timeout: float = 0.0
    ) -> list[CanFrame]:
        """Envía una petición y devuelve los frames de respuesta de la ECU.

        ``timeout`` es cuánto esperar con ``clock.sleep`` a una respuesta
        retrasada o incompleta; 0 devuelve lo disponible tras procesarla.
        """
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("timeout debe ser finito y no negativo")
        # A virtual bus is shared state; serialize request/response transactions.
        with self._exchange_lock:
            return self._exchange_frames(bus_name, request, timeout)

    def _exchange_frames(
        self, bus_name: str, request: CanFrame, timeout: float
    ) -> list[CanFrame]:
        if self.state is not VehicleState.RUNNING:
            raise VehicleStateError("el vehículo no está ejecutándose")
        self.tick()
        bus = self.buses[bus_name]
        if isinstance(bus, VirtualCanBus):
            return self._virtual_exchange_frames(bus_name, bus, request, timeout)
        bus.send(request)
        received = bus.recv(timeout=0)
        if received is None:
            return []
        for ecu in self.ecus:
            if not self._can_exchange(ecu, bus_name) or self._silent(ecu):
                continue
            responses = ecu.handle_frames(received)
            if responses is not None:
                # Preserve the complete-response convenience API. The client
                # explicitly acknowledges FF/blocks through FC on the bus.
                client = IsoTpTransport(
                    ecu.request_id, ecu.response_id, clock=self.clock
                )
                result: list[CanFrame] = []
                while responses:
                    self._emit(ecu, responses, bus.send)
                    responses = list[CanFrame]()
                    while (response := bus.recv(timeout=0)) is not None:
                        result.append(response)
                        if response.data and response.data[0] >> 4 != 3:
                            try:
                                client.receive(response)
                            except IsoTpError:
                                return result
                    for control in client.poll():
                        bus.send(control)
                        received_control = bus.recv(timeout=0)
                        if received_control is not None:
                            responses.extend(ecu.handle_frames(received_control) or [])
                return result

        return []

    def _virtual_exchange_frames(
        self, bus_name: str, bus: VirtualCanBus, request: CanFrame, timeout: float
    ) -> list[CanFrame]:
        target = next(
            (
                ecu
                for ecu in self.ecus
                if ecu.request_id == request.arbitration_id
                and not request.is_extended_id
                and ecu.online
                and self._can_exchange(ecu, bus_name)
                and not self._silent(ecu)
            ),
            None,
        )
        filters = [CanFilter(target.response_id, 0x7FF, False)] if target else []
        driver = bus.connect(filters=filters)
        deadline = self.clock.now() + timeout
        try:
            driver.send(request)
            self.tick()
            if target is None:
                return []
            client = IsoTpTransport(
                target.request_id, target.response_id, clock=self.clock
            )
            result: list[CanFrame] = []
            while True:
                while (response := driver.recv(timeout=0)) is not None:
                    result.append(response)
                    if response.data and response.data[0] >> 4 != 3:
                        try:
                            client.receive(response)
                        except IsoTpError:
                            return result
                try:
                    controls = client.poll()
                except IsoTpError:
                    return result
                for control in controls:
                    driver.send(control)
                waiting = not result or client.receiver.receiving
                if not controls and not (waiting and self._wait(deadline)):
                    return result
                self.tick()
        finally:
            driver.close()

    def _wait(self, deadline: float) -> bool:
        """Duerme hasta el próximo frame retrasado o 1 ms; False si expiró."""
        now = self.clock.now()
        if now >= deadline:
            return False
        step = 0.001
        if self._delayed:
            step = max(self._delayed[0][0] - now, 0.0) or step
        self.clock.sleep(min(step, deadline - now))
        return True

    def exchange_isotp(
        self, bus_name: str, request: CanFrame, timeout: float = 0.0
    ) -> bytes | None:
        """Como ``exchange_frames`` pero reensambla el payload ISO-TP."""
        responses = self.exchange_frames(bus_name, request, timeout)
        return reassemble(responses) if responses else None
