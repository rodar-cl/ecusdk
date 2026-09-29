"""Codec ISO-TP básico sobre frames CAN."""

from __future__ import annotations

import math
from enum import IntEnum

from ecusdk.can import CanFrame
from ecusdk.clock import Clock, RealClock, VirtualClock
from ecusdk.errors import IsoTpError, IsoTpTimeoutError


def segment(
    payload: bytes, arbitration_id: int, *, is_extended_id: bool = False
) -> list[CanFrame]:
    """Segmenta un payload ISO-TP en frames CAN 2.0."""
    if len(payload) > 4095:
        raise IsoTpError("el payload ISO-TP excede 4095 bytes")
    if len(payload) <= 7:
        return [
            CanFrame(arbitration_id, bytes((len(payload),)) + payload, is_extended_id)
        ]
    frames = [
        CanFrame(
            arbitration_id,
            bytes((0x10 | (len(payload) >> 8), len(payload) & 0xFF)) + payload[:6],
            is_extended_id,
        )
    ]
    for sequence, offset in enumerate(range(6, len(payload), 7), start=1):
        frames.append(
            CanFrame(
                arbitration_id,
                bytes((0x20 | (sequence & 0x0F),)) + payload[offset : offset + 7],
                is_extended_id,
            )
        )
    return frames


def reassemble(frames: list[CanFrame]) -> bytes:
    """Reensambla frames ISO-TP y valida longitud y secuencia."""
    if not frames:
        raise IsoTpError("se requiere al menos un frame")
    receiver = IsoTpReceiver(clock=VirtualClock())
    for index, frame in enumerate(frames):
        if index and frame.data and frame.data[0] >> 4 != 2:
            raise IsoTpError("se esperaba un Consecutive Frame")
        result = receiver.push(frame)
        if result is not None:
            if index != len(frames) - 1:
                raise IsoTpError("frames ISO-TP posteriores al payload")
            return result
    raise IsoTpError("transferencia ISO-TP incompleta")


class IsoTpReceiver:
    """Receptor ISO-TP con timeout entre el First Frame y cada CF."""

    def __init__(
        self,
        timeout: float = 1.0,
        clock: Clock | None = None,
    ) -> None:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout debe ser positivo")
        self.timeout = timeout
        self.clock = clock or RealClock()
        self._expected_length: int | None = None
        self._buffer = bytearray()
        self._sequence = 1
        self._deadline: float | None = None
        self._address: tuple[int, bool] | None = None

    def push(self, frame: CanFrame) -> bytes | None:
        """Acepta un frame y devuelve el payload cuando la transferencia termina."""
        try:
            return self._push(frame)
        except IsoTpError:
            self._reset()
            raise

    @property
    def receiving(self) -> bool:
        """Indica si hay una transferencia multi-frame pendiente."""
        return self._expected_length is not None

    def _push(self, frame: CanFrame) -> bytes | None:
        self.check_timeout()
        if not frame.data:
            raise IsoTpError("frame ISO-TP vacío")
        frame_type = frame.data[0] >> 4
        if frame_type == 0:
            length = frame.data[0] & 0x0F
            if length > len(frame.data) - 1:
                raise IsoTpError("Single Frame inválido")
            self._reset()
            return frame.data[1 : length + 1]
        if frame_type == 1:
            if len(frame.data) != 8:
                raise IsoTpError("First Frame inválido")
            self._expected_length = ((frame.data[0] & 0x0F) << 8) | frame.data[1]
            if self._expected_length <= 7:
                raise IsoTpError("First Frame debe exceder el tamaño de Single Frame")
            self._address = (frame.arbitration_id, frame.is_extended_id)
            self._buffer = bytearray(frame.data[2:])
            self._sequence = 1
            self._deadline = self.clock.now() + self.timeout
            return self._finish_if_complete()
        if frame_type != 2 or self._expected_length is None:
            raise IsoTpError("Consecutive Frame inesperado")
        if (frame.arbitration_id, frame.is_extended_id) != self._address:
            raise IsoTpError("dirección ISO-TP inconsistente")
        remaining = self._expected_length - len(self._buffer)
        if len(frame.data) - 1 < min(7, remaining):
            raise IsoTpError("Consecutive Frame truncado")
        if frame.data[0] & 0x0F != self._sequence:
            raise IsoTpError("secuencia ISO-TP inválida")
        self._buffer.extend(frame.data[1:])
        self._sequence = (self._sequence + 1) & 0x0F
        self._deadline = self.clock.now() + self.timeout
        return self._finish_if_complete()

    def check_timeout(self) -> None:
        if self._deadline is not None and self.clock.now() >= self._deadline:
            self._reset()
            raise IsoTpTimeoutError("timeout esperando un frame ISO-TP")

    def _finish_if_complete(self) -> bytes | None:
        if self._expected_length is None or len(self._buffer) < self._expected_length:
            return None
        result = bytes(self._buffer[: self._expected_length])
        self._reset()
        return result

    def _reset(self) -> None:
        self._expected_length = None
        self._buffer.clear()
        self._deadline = None
        self._address = None


class FlowStatus(IntEnum):
    """Estado anunciado por un frame Flow Control."""

    CONTINUE = 0
    WAIT = 1
    OVERFLOW = 2


def separation_seconds(value: int) -> float:
    """Decodifica STmin; rechaza valores reservados con IsoTpError."""
    if 0 <= value <= 0x7F:
        return value / 1000
    if 0xF1 <= value <= 0xF9:
        return (value - 0xF0) / 10000
    raise IsoTpError("STmin reservado")


def decode_flow_control(frame: CanFrame) -> tuple[FlowStatus, int, float]:
    """Decodifica estado, block size y STmin de un FC válido."""
    if len(frame.data) < 3 or frame.data[0] >> 4 != 3:
        raise IsoTpError("Flow Control inválido")
    try:
        status = FlowStatus(frame.data[0] & 0x0F)
    except ValueError as error:
        raise IsoTpError("estado Flow Control inválido") from error
    return status, frame.data[1], separation_seconds(frame.data[2])


def flow_control(
    arbitration_id: int,
    block_size: int = 0,
    separation_time: int = 0,
    *,
    status: FlowStatus = FlowStatus.CONTINUE,
    is_extended_id: bool = False,
) -> CanFrame:
    """Construye FC; valida BS/STmin y admite CTS, WAIT y OVERFLOW."""
    if not 0 <= block_size <= 255 or not 0 <= separation_time <= 255:
        raise ValueError("block_size y separation_time deben estar entre 0 y 255")
    separation_seconds(separation_time)
    return CanFrame(
        arbitration_id,
        bytes((0x30 | FlowStatus(status), block_size, separation_time)),
        is_extended_id,
    )
