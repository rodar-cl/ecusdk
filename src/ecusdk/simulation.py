"""Runtime mínimo de vehículos, ECUs y escenarios deterministas."""

from __future__ import annotations

import json
import math
import threading
from collections.abc import Iterator, MutableMapping, Sequence
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, TypeAlias

from ecusdk.can import CanBus, CanFilter, CanFrame, VirtualCanBus, VirtualCanNode
from ecusdk.clock import Clock, RealClock
from ecusdk.errors import IsoTpError, ObdError
from ecusdk.isotp import reassemble
from ecusdk.isotp_transport import IsoTpTransport
from ecusdk.obd import DtcStore, ObdRegistry, encode_dtc

SignalValue: TypeAlias = int | float


class VehicleState(Enum):
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
        return self._values[key]

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
    _initial_signals: dict[str, SignalValue] = field(init=False, repr=False)
    _transport: IsoTpTransport = field(init=False, repr=False)
    obd: ObdRegistry = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.signals = SignalMap(dict(self.signals), self.signal_limits)
        self._initial_signals = dict(self.signals)
        self._transport = IsoTpTransport(self.response_id, self.request_id)
        self.obd = ObdRegistry(self.signals)

    def reset(self) -> None:
        self._transport.reset()
        for name, value in self._initial_signals.items():
            self.signals[name] = value

    def set_protocol_clock(self, clock: Clock) -> None:
        """Asigna el reloj de transporte y cancela transferencias pendientes."""
        self._transport = IsoTpTransport(self.response_id, self.request_id, clock=clock)

    def get_signal(self, name: str) -> SignalValue:
        try:
            return self.signals[name]
        except KeyError as error:
            raise KeyError(f"señal desconocida: {name}") from error

    def set_signal(self, name: str, value: SignalValue) -> None:
        self.signals[name] = value

    def obd_payload(self, request: bytes) -> bytes | None:
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
        if not self.online or frame.arbitration_id != self.request_id:
            return None
        try:
            payload = self._transport.receive(frame)
            if payload is not None:
                response_payload = self.obd_payload(payload)
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
        frames = self.handle_frames(frame)
        return frames[0] if frames else None


@dataclass(frozen=True, slots=True)
class ScenarioEvent:
    at: float
    ecu: str
    action: str
    signal: str | None = None
    value: SignalValue | None = None
    duration: float = 0.0


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
        return ScenarioEvent(
            at=float(event.get("at", event.get("time", 0))),
            ecu=str(event["ecu"]),
            action=str(event["action"]),
            signal=event.get("signal"),
            value=event.get("value"),
            duration=float(event.get("duration", 0)),
        )

    @property
    def started(self) -> bool:
        return self._origin is not None

    def start(self, now: float) -> None:
        self._origin = now

    def reset(self) -> None:
        self._cursor = 0
        self._origin = None
        self._paused_at = None

    def pause(self, now: float) -> None:
        self._paused_at = now

    def resume(self, now: float) -> None:
        if self._paused_at is not None and self._origin is not None:
            self._origin += now - self._paused_at
        self._paused_at = None

    def due(self, now: float) -> list[ScenarioEvent]:
        if self._origin is None:
            self.start(now)
        origin = self._origin
        assert origin is not None
        elapsed = now - origin
        if self._paused_at is not None:
            elapsed = self._paused_at - origin
        due: list[ScenarioEvent] = []
        while (
            self._cursor < len(self.events) and self.events[self._cursor].at <= elapsed
        ):
            due.append(self.events[self._cursor])
            self._cursor += 1
        return due

    def to_json(self) -> str:
        return json.dumps([asdict(event) for event in self.events], sort_keys=True)

    @classmethod
    def from_json(cls, value: str) -> Scenario:
        return cls(json.loads(value))


class Vehicle:
    def __init__(
        self,
        name: str,
        buses: dict[str, CanBus],
        ecus: list[ECU],
        clock: Clock | None = None,
        scenario: Scenario | None = None,
    ) -> None:
        self.name, self.buses, self.ecus = name, buses, ecus
        self.clock = clock or RealClock()
        self.state = VehicleState.STOPPED
        for ecu in self.ecus:
            ecu.set_protocol_clock(self.clock)
        self.scenario = scenario
        self._offline_until: dict[str, float] = {}
        self._timeout_until: dict[str, float] = {}
        self._exchange_lock = threading.RLock()
        self._virtual_nodes: dict[str, list[tuple[ECU, VirtualCanNode]]] = {}
        self._protocol_buses: dict[int, str] = {}
        for bus in self.buses.values():
            if isinstance(bus, VirtualCanBus):
                bus.set_clock(self.clock)

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

    def start(self) -> None:
        with self._exchange_lock:
            self._connect_virtual_nodes()
            self.state = VehicleState.RUNNING
            if self.scenario is not None and not self.scenario.started:
                self.scenario.start(self.clock.now())

    def stop(self) -> None:
        with self._exchange_lock:
            self.state = VehicleState.STOPPED
            self._disconnect_virtual_nodes()
            for ecu in self.ecus:
                ecu.set_protocol_clock(self.clock)

    def pause(self) -> None:
        if self.state is VehicleState.RUNNING:
            if self.scenario is not None:
                self.scenario.pause(self.clock.now())
            self.state = VehicleState.PAUSED

    def resume(self) -> None:
        if self.state is VehicleState.PAUSED:
            if self.scenario is not None:
                self.scenario.resume(self.clock.now())
            self.state = VehicleState.RUNNING

    def reset(self) -> None:
        self.stop()
        self._offline_until.clear()
        self._timeout_until.clear()
        for ecu in self.ecus:
            ecu.online = True
            ecu.reset()
        if self.scenario is not None:
            self.scenario.reset()

    def tick(self) -> None:
        """Avanza escenarios y procesa un frame por nodo ECU y sus timers."""
        with self._exchange_lock:
            if self.state is not VehicleState.RUNNING:
                return
            if self.scenario is not None:
                for event in self.scenario.due(self.clock.now()):
                    ecu = next(
                        (item for item in self.ecus if item.name == event.ecu), None
                    )
                    if ecu is None:
                        raise KeyError(f"ECU desconocida: {event.ecu}")
                    if event.action in ("set", "signal"):
                        if event.signal is None or event.value is None:
                            raise ValueError("evento de señal incompleto")
                        ecu.set_signal(event.signal, event.value)
                    elif event.action in ("offline", "ecu_offline"):
                        self._offline_until[ecu.name] = (
                            self.clock.now() + event.duration
                        )
                    elif event.action == "timeout":
                        self._timeout_until[ecu.name] = (
                            self.clock.now() + event.duration
                        )

            self._service_virtual_buses()

    def _service_virtual_buses(self) -> None:
        for bus_name, ports in self._virtual_nodes.items():
            for ecu, port in ports:
                frame = port.recv(timeout=0)
                if not self._can_exchange(
                    ecu, bus_name
                ) or self.clock.now() < self._timeout_until.get(ecu.name, -1):
                    continue
                if frame is not None:
                    self._protocol_buses[id(ecu)] = bus_name
                    for response in ecu.handle_frames(frame) or []:
                        port.send(response)
                if self._protocol_buses.get(id(ecu), bus_name) == bus_name:
                    for response in ecu.poll_frames():
                        port.send(response)

    def inject_timeout(self, ecu: str, duration: float) -> None:
        self._timeout_until[ecu] = self.clock.now() + duration

    def set_ecu_offline(self, ecu: str, duration: float) -> None:
        self._offline_until[ecu] = self.clock.now() + duration

    def _can_exchange(self, ecu: ECU, bus_name: str) -> bool:
        return (
            ecu.bus is None or ecu.bus == bus_name
        ) and self.clock.now() >= self._offline_until.get(ecu.name, -1)

    def exchange(self, bus_name: str, request: CanFrame) -> CanFrame | None:
        responses = self.exchange_frames(bus_name, request)
        return responses[0] if responses else None

    def exchange_frames(self, bus_name: str, request: CanFrame) -> list[CanFrame]:
        # A virtual bus is shared state; serialize request/response transactions.
        with self._exchange_lock:
            return self._exchange_frames(bus_name, request)

    def _exchange_frames(self, bus_name: str, request: CanFrame) -> list[CanFrame]:
        if self.state is not VehicleState.RUNNING:
            raise RuntimeError("el vehículo no está ejecutándose")
        self.tick()
        bus = self.buses[bus_name]
        if isinstance(bus, VirtualCanBus):
            return self._virtual_exchange_frames(bus_name, bus, request)
        bus.send(request)
        received = bus.recv(timeout=0)
        if received is None:
            return []
        for ecu in self.ecus:
            if (
                not self._can_exchange(ecu, bus_name)
                or ecu.name in self._timeout_until
                and self.clock.now() < self._timeout_until[ecu.name]
            ):
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
                    for response in responses:
                        bus.send(response)
                    responses = list[CanFrame]()
                    while (response := bus.recv(timeout=0)) is not None:
                        result.append(response)
                        if response.data and response.data[0] >> 4 != 3:
                            client.receive(response)
                    for control in client.poll():
                        bus.send(control)
                        received_control = bus.recv(timeout=0)
                        if received_control is not None:
                            responses.extend(ecu.handle_frames(received_control) or [])
                return result

        return []

    def _virtual_exchange_frames(
        self, bus_name: str, bus: VirtualCanBus, request: CanFrame
    ) -> list[CanFrame]:
        target = next(
            (
                ecu
                for ecu in self.ecus
                if ecu.request_id == request.arbitration_id
                and not request.is_extended_id
                and ecu.online
                and self._can_exchange(ecu, bus_name)
                and self.clock.now() >= self._timeout_until.get(ecu.name, -1)
            ),
            None,
        )
        filters = [CanFilter(target.response_id, 0x7FF, False)] if target else []
        driver = bus.connect(filters=filters)
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
                        client.receive(response)
                controls = client.poll()
                if not controls:
                    return result
                for control in controls:
                    driver.send(control)
                self.tick()
        finally:
            driver.close()

    def exchange_isotp(self, bus_name: str, request: CanFrame) -> bytes | None:
        responses = self.exchange_frames(bus_name, request)
        return reassemble(responses) if responses else None
