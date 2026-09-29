"""Codec ISO-TP básico sobre frames CAN."""

from __future__ import annotations

from ecusdk.can import CanFrame
from ecusdk.clock import Clock, RealClock
from ecusdk.errors import IsoTpError, IsoTpTimeoutError


def segment(payload: bytes, arbitration_id: int) -> list[CanFrame]:
    """Segmenta un payload ISO-TP en frames CAN 2.0."""
    if len(payload) > 4095:
        raise IsoTpError("el payload ISO-TP excede 4095 bytes")
    if len(payload) <= 7:
        return [CanFrame(arbitration_id, bytes((len(payload),)) + payload)]
    frames = [
        CanFrame(
            arbitration_id,
            bytes((0x10 | (len(payload) >> 8), len(payload) & 0xFF)) + payload[:6],
        )
    ]
    for sequence, offset in enumerate(range(6, len(payload), 7), start=1):
        frames.append(
            CanFrame(
                arbitration_id,
                bytes((0x20 | (sequence & 0x0F),)) + payload[offset : offset + 7],
            )
        )
    return frames


def reassemble(frames: list[CanFrame]) -> bytes:
    """Reensambla frames ISO-TP y valida longitud y secuencia."""
    if not frames:
        raise IsoTpError("se requiere al menos un frame")
    first = frames[0].data
    if not first:
        raise IsoTpError("frame ISO-TP vacío")
    frame_type = first[0] >> 4
    if frame_type == 0:
        length = first[0] & 0x0F
        if length > len(first) - 1 or len(frames) != 1:
            raise IsoTpError("Single Frame inválido")
        return first[1 : length + 1]
    if frame_type != 1 or len(first) < 2:
        raise IsoTpError("First Frame inválido")
    length = ((first[0] & 0x0F) << 8) | first[1]
    if length <= 7:
        raise IsoTpError("First Frame debe exceder el tamaño de Single Frame")
    result = bytearray(first[2:])
    expected_sequence = 1
    for frame in frames[1:]:
        data = frame.data
        if not data or data[0] >> 4 != 2 or data[0] & 0x0F != expected_sequence:
            raise IsoTpError("secuencia ISO-TP inválida")
        result.extend(data[1:])
        expected_sequence = (expected_sequence + 1) & 0x0F
        if len(result) >= length:
            if frame is not frames[-1]:
                raise IsoTpError("frames ISO-TP posteriores al payload")
            break
    if len(result) < length:
        raise IsoTpError("transferencia ISO-TP incompleta")
    return bytes(result)


class IsoTpReceiver:
    """Receptor ISO-TP con timeout entre el First Frame y cada CF."""

    def __init__(
        self,
        timeout: float = 1.0,
        clock: Clock | None = None,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout debe ser positivo")
        self.timeout = timeout
        self.clock = clock or RealClock()
        self._expected_length: int | None = None
        self._buffer = bytearray()
        self._sequence = 1
        self._deadline: float | None = None

    def push(self, frame: CanFrame) -> bytes | None:
        """Acepta un frame y devuelve el payload cuando la transferencia termina."""
        self.check_timeout()
        if not frame.data:
            raise IsoTpError("frame ISO-TP vacío")
        frame_type = frame.data[0] >> 4
        if frame_type == 0:
            length = frame.data[0] & 0x0F
            if length > len(frame.data) - 1:
                raise IsoTpError("Single Frame inválido")
            return frame.data[1 : length + 1]
        if frame_type == 1:
            if len(frame.data) < 2:
                raise IsoTpError("First Frame inválido")
            self._expected_length = ((frame.data[0] & 0x0F) << 8) | frame.data[1]
            if self._expected_length <= 7:
                raise IsoTpError("First Frame debe exceder el tamaño de Single Frame")
            self._buffer = bytearray(frame.data[2:])
            self._sequence = 1
            self._deadline = self.clock.now() + self.timeout
            return self._finish_if_complete()
        if frame_type != 2 or self._expected_length is None:
            raise IsoTpError("Consecutive Frame inesperado")
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


def flow_control(
    arbitration_id: int, block_size: int = 0, separation_time: int = 0
) -> CanFrame:
    if not 0 <= block_size <= 255 or not 0 <= separation_time <= 255:
        raise ValueError("block_size y separation_time deben estar entre 0 y 255")
    return CanFrame(arbitration_id, bytes((0x30, block_size, separation_time)))
