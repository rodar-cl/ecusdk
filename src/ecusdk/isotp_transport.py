"""Transporte ISO-TP CAN clásico, normal addressing, sin sleeps ni I/O implícito."""

from __future__ import annotations

import math

from ecusdk.can import CanBus, CanFrame
from ecusdk.clock import Clock, RealClock
from ecusdk.errors import IsoTpError, IsoTpTimeoutError
from ecusdk.isotp import (
    FlowStatus,
    IsoTpReceiver,
    decode_flow_control,
    flow_control,
    segment,
)


class IsoTpSender:
    """Máquina de envío: FF, espera FC, bloques de CF y STmin.

    El llamador entrega FC mediante ``accept`` y emite los frames de ``poll``.
    ``poll`` usa Clock; nunca duerme. Timeout/overflow cancelan la transferencia.
    """

    def __init__(
        self,
        arbitration_id: int,
        *,
        clock: Clock | None = None,
        timeout: float = 1.0,
        max_wait_frames: int = 3,
        is_extended_id: bool = False,
    ) -> None:
        CanFrame(arbitration_id, b"", is_extended_id)
        if not math.isfinite(timeout) or timeout <= 0 or max_wait_frames < 0:
            raise ValueError("timeout debe ser positivo y max_wait_frames no negativo")
        self.arbitration_id = arbitration_id
        self.is_extended_id = is_extended_id
        self.clock = clock or RealClock()
        self.timeout = timeout
        self.max_wait_frames = max_wait_frames
        self.reset()

    def reset(self) -> None:
        """Cancela el envío y libera frames y timers."""
        self._frames: list[CanFrame] = []
        self._index = 0
        self._deadline: float | None = None
        self._next_at = 0.0
        self._last_at: float | None = None
        self._remaining = 0
        self._stmin = 0.0
        self._wait_count = 0

    @property
    def busy(self) -> bool:
        """Indica si quedan CF pendientes de emisión."""
        return self._index < len(self._frames)

    def start(self, payload: bytes) -> list[CanFrame]:
        """Inicia un payload y devuelve sólo SF o FF; falla si ya hay envío."""
        if self.busy:
            raise IsoTpError("ya hay un envío ISO-TP activo")
        frames = segment(
            payload, self.arbitration_id, is_extended_id=self.is_extended_id
        )
        self.reset()
        if len(frames) > 1:
            self._frames = frames
            self._index = 1
            self._deadline = self.clock.now() + self.timeout
        return frames[:1]

    def check_timeout(self) -> None:
        """Comprueba el deadline de Flow Control y cancela al expirar."""
        if self._deadline is not None and self.clock.now() >= self._deadline:
            self.reset()
            raise IsoTpTimeoutError("timeout esperando Flow Control")

    def accept(self, frame: CanFrame) -> None:
        """Procesa CTS, WAIT u OVERFLOW; FC inválido cancela el envío."""
        try:
            self.check_timeout()
            if not self.busy or self._deadline is None:
                raise IsoTpError("Flow Control inesperado")
            status, block_size, stmin = decode_flow_control(frame)
            if status is FlowStatus.OVERFLOW:
                raise IsoTpError("receptor ISO-TP en overflow")
            if status is FlowStatus.WAIT:
                self._wait_count += 1
                if self._wait_count > self.max_wait_frames:
                    raise IsoTpError("demasiados Flow Control WAIT")
                self._deadline = self.clock.now() + self.timeout
                return
            self._wait_count = 0
            self._deadline = None
            self._remaining = block_size or len(self._frames)
            self._stmin = stmin
            self._next_at = max(
                self.clock.now(),
                (self._last_at + stmin) if self._last_at is not None else 0.0,
            )
        except IsoTpError:
            self.reset()
            raise

    def poll(self) -> list[CanFrame]:
        """Devuelve CF habilitados por FC y el reloj, sin anticipar STmin."""
        self.check_timeout()
        result: list[CanFrame] = []
        now = self.clock.now()
        while self.busy and self._deadline is None and now >= self._next_at:
            result.append(self._frames[self._index])
            self._index += 1
            self._remaining -= 1
            self._last_at = now
            self._next_at = now + self._stmin
            if not self.busy:
                self.reset()
            elif self._remaining == 0:
                self._deadline = now + self.timeout
        return result


class IsoTpTransport:
    """Endpoint con direcciones explícitas y máquinas RX/TX independientes.

    ``send`` inicia TX; ``receive`` procesa un frame entrante y devuelve un
    payload completo o None; ``poll`` devuelve frames salientes. Opcionalmente
    ``pump`` intercambia esos frames con un CanBus proporcionado por el usuario.
    Normal addressing sobre CAN clásico, payloads de hasta 4095 bytes.
    """

    def __init__(
        self,
        tx_id: int,
        rx_id: int,
        *,
        clock: Clock | None = None,
        timeout: float = 1.0,
        block_size: int = 0,
        separation_time: int = 0,
        max_wait_frames: int = 3,
        max_payload: int = 4095,
        is_extended_id: bool = False,
    ) -> None:
        CanFrame(rx_id, b"", is_extended_id)
        flow_control(tx_id, block_size, separation_time, is_extended_id=is_extended_id)
        if not 8 <= max_payload <= 4095:
            raise ValueError("max_payload debe estar entre 8 y 4095")
        self.tx_id, self.rx_id = tx_id, rx_id
        self.is_extended_id = is_extended_id
        self.clock = clock or RealClock()
        self.block_size, self.separation_time = block_size, separation_time
        self.max_payload = max_payload
        self.sender = IsoTpSender(
            tx_id,
            clock=self.clock,
            timeout=timeout,
            max_wait_frames=max_wait_frames,
            is_extended_id=is_extended_id,
        )
        self.receiver = IsoTpReceiver(timeout=timeout, clock=self.clock)
        self._outgoing: list[CanFrame] = []
        self._received_in_block = 0

    def reset(self) -> None:
        """Cancela RX/TX y descarta frames aún no emitidos."""
        self.sender.reset()
        self.receiver = IsoTpReceiver(self.receiver.timeout, self.clock)
        self._outgoing.clear()
        self._received_in_block = 0

    def send(self, payload: bytes) -> None:
        """Encola SF/FF. Los CF esperan FC; IsoTpError indica envío activo."""
        self._outgoing.extend(self.sender.start(payload))

    def receive(self, frame: CanFrame) -> bytes | None:
        """Procesa RX/FC de rx_id; ignora otras direcciones.

        Malformaciones y timeout producen IsoTpError y cancelan RX/TX. Una
        longitud superior al buffer encola OVERFLOW y descarta ese mensaje.
        """
        if (
            frame.arbitration_id != self.rx_id
            or frame.is_extended_id != self.is_extended_id
        ):
            return None
        try:
            self.receiver.check_timeout()
            self.sender.check_timeout()
            if frame.data and frame.data[0] >> 4 == 3:
                self.sender.accept(frame)
                return None
            if len(frame.data) == 8 and frame.data[0] >> 4 == 1:
                length = ((frame.data[0] & 15) << 8) | frame.data[1]
                if length > self.max_payload:
                    self.receiver = IsoTpReceiver(self.receiver.timeout, self.clock)
                    self._received_in_block = 0
                    self._outgoing.append(
                        flow_control(
                            self.tx_id,
                            status=FlowStatus.OVERFLOW,
                            is_extended_id=self.is_extended_id,
                        )
                    )
                    return None
            result = self.receiver.push(frame)
            if self.receiver.receiving:
                if frame.data[0] >> 4 == 1:
                    self._received_in_block = 0
                    self._outgoing.append(
                        flow_control(
                            self.tx_id,
                            self.block_size,
                            self.separation_time,
                            is_extended_id=self.is_extended_id,
                        )
                    )
                else:
                    self._received_in_block += 1
                    if self.block_size and self._received_in_block == self.block_size:
                        self._received_in_block = 0
                        self._outgoing.append(
                            flow_control(
                                self.tx_id,
                                self.block_size,
                                self.separation_time,
                                is_extended_id=self.is_extended_id,
                            )
                        )
            return result
        except IsoTpError:
            self.reset()
            raise

    def poll(self) -> list[CanFrame]:
        """Obtiene frames pendientes y comprueba timers RX/TX."""
        try:
            self.receiver.check_timeout()
            frames = self._outgoing + self.sender.poll()
            self._outgoing = []
            return frames
        except IsoTpError:
            self.reset()
            raise

    def pump(self, bus: CanBus) -> list[bytes]:
        """Lee frames disponibles sin bloquear y envía frames habilitados.

        CanBus debe representar el endpoint del llamador y no reflejar sus
        propios envíos como entradas. Devuelve los payloads recibidos.
        """
        payloads: list[bytes] = []
        while (frame := bus.recv(timeout=0)) is not None:
            payload = self.receive(frame)
            if payload is not None:
                payloads.append(payload)
        for frame in self.poll():
            bus.send(frame)
        return payloads
