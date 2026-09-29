"""Contratos ISO-TP y regresiones de la revisión de Fase 2."""

from collections import deque

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ecusdk import CanFrame, IsoTpSender, IsoTpTransport, VirtualClock
from ecusdk.errors import IsoTpError, IsoTpTimeoutError
from ecusdk.isotp import (
    FlowStatus,
    IsoTpReceiver,
    decode_flow_control,
    flow_control,
    reassemble,
    segment,
    separation_seconds,
)
from ecusdk.simulation import ECU


@given(st.binary(min_size=0, max_size=4095), st.integers(0, 255))
def test_padded_round_trip(payload: bytes, padding: int) -> None:
    frames = [
        CanFrame(f.arbitration_id, f.data.ljust(8, bytes((padding,))))
        for f in segment(payload, 0x700)
    ]
    assert reassemble(frames) == payload


@given(st.binary(min_size=0, max_size=4095), st.integers(0, 20), st.booleans())
def test_peer_transport_round_trip(
    payload: bytes, block_size: int, extended: bool
) -> None:
    clock = VirtualClock()
    tx_id, rx_id = (0x18DA0100, 0x18DA0001) if extended else (0x700, 0x701)
    sender = IsoTpTransport(tx_id, rx_id, clock=clock, is_extended_id=extended)
    receiver = IsoTpTransport(
        rx_id, tx_id, clock=clock, block_size=block_size, is_extended_id=extended
    )
    sender.send(payload)
    result: bytes | None = None
    for _ in range(600):
        outgoing = sender.poll()
        for frame in outgoing:
            received = receiver.receive(frame)
            if received is not None:
                result = received
        for frame in receiver.poll():
            sender.receive(frame)
        if result is not None:
            break
    assert result == payload
    assert not sender.sender.busy


def test_vin_requires_cts_and_preserves_convenience_api() -> None:
    ecu = ECU("ECM", 0x7E0, 0x7E8, vin="ECUSDK00000000001")
    first = ecu.handle_frames(CanFrame(0x7E0, bytes.fromhex("02 09 02")))
    assert first is not None and len(first) == 1
    assert first[0].data == bytes.fromhex("10 14 49 02 01 45 43 55")
    rest = ecu.handle_frames(flow_control(0x7E0))
    assert rest is not None and len(rest) == 2
    assert reassemble(first + rest) == bytes.fromhex("49 02 01") + ecu.vin.encode()


@pytest.mark.parametrize("stmin,seconds", [(20, 0.02), (0xF1, 0.0001), (0xF9, 0.0009)])
def test_sender_respects_blocks_and_stmin(stmin: int, seconds: float) -> None:
    clock = VirtualClock()
    sender = IsoTpSender(0x700, clock=clock)
    assert len(sender.start(b"x" * 34)) == 1
    assert sender.poll() == []
    sender.accept(flow_control(0x701, 2, stmin))
    assert len(sender.poll()) == 1
    assert sender.poll() == []
    clock.advance(seconds)
    assert len(sender.poll()) == 1
    clock.advance(seconds)
    assert sender.poll() == []  # block exhausted; needs another FC
    sender.accept(flow_control(0x701, 0, stmin))
    assert len(sender.poll()) == 1
    clock.advance(seconds)
    assert len(sender.poll()) == 1
    assert not sender.busy


def test_wait_limit_timeout_overflow_and_reuse() -> None:
    clock = VirtualClock()
    sender = IsoTpSender(0x700, clock=clock, timeout=1, max_wait_frames=1)
    sender.start(b"x" * 20)
    clock.advance(0.5)
    sender.accept(flow_control(0x701, status=FlowStatus.WAIT))
    clock.advance(0.5)
    assert sender.poll() == []  # WAIT refreshed the deadline
    with pytest.raises(IsoTpError, match="demasiados"):
        sender.accept(flow_control(0x701, status=FlowStatus.WAIT))
    assert not sender.busy
    sender.start(b"x" * 20)
    with pytest.raises(IsoTpError, match="overflow"):
        sender.accept(flow_control(0x701, status=FlowStatus.OVERFLOW))
    sender.start(b"x" * 20)
    clock.advance(1)
    with pytest.raises(IsoTpTimeoutError):
        sender.poll()
    assert sender.start(b"ok")[0].data == b"\x02ok"


def test_block_deadline_and_late_fc() -> None:
    clock = VirtualClock()
    sender = IsoTpSender(0x700, clock=clock)
    sender.start(b"x" * 20)
    with pytest.raises(IsoTpError, match="activo"):
        sender.start(b"new")
    sender.accept(flow_control(0x701, 1))
    assert len(sender.poll()) == 1
    clock.advance(1)
    with pytest.raises(IsoTpTimeoutError):
        sender.accept(flow_control(0x701))


@pytest.mark.parametrize(
    "data", [b"", b"\x30", b"\x00\x00\x00", b"\x33\x00\x00", b"\x30\x00\x80"]
)
def test_malformed_fc_cancels_transfer(data: bytes) -> None:
    sender = IsoTpSender(0x700, clock=VirtualClock())
    sender.start(b"x" * 20)
    with pytest.raises(IsoTpError):
        sender.accept(CanFrame(0x701, data))
    assert not sender.busy


def test_unexpected_fc_and_reserved_stmin() -> None:
    sender = IsoTpSender(0x700)
    with pytest.raises(IsoTpError, match="inesperado"):
        sender.accept(flow_control(0x701))
    assert decode_flow_control(flow_control(0x701, 8, 20)) == (
        FlowStatus.CONTINUE,
        8,
        0.02,
    )
    assert separation_seconds(0x7F) == 0.127
    with pytest.raises(IsoTpError):
        flow_control(0x701, separation_time=0xFA)
    with pytest.raises(ValueError):
        flow_control(0x701, block_size=256)


def test_receive_overflow_wrong_address_and_recovery() -> None:
    endpoint = IsoTpTransport(0x701, 0x700, max_payload=8)
    assert endpoint.receive(CanFrame(0x702, b"")) is None
    assert endpoint.receive(CanFrame(0x700, b"", is_extended_id=True)) is None
    endpoint.receive(segment(b"x" * 20, 0x700)[0])
    assert decode_flow_control(endpoint.poll()[0])[0] is FlowStatus.OVERFLOW
    assert endpoint.receive(segment(b"ok", 0x700)[0]) == b"ok"
    with pytest.raises(IsoTpError):
        endpoint.receive(CanFrame(0x700, b""))
    assert endpoint.receive(segment(b"ok", 0x700)[0]) == b"ok"


def test_receive_timeout_and_duplicate_cf_cancel_transfer() -> None:
    clock = VirtualClock()
    endpoint = IsoTpTransport(0x701, 0x700, clock=clock)
    frames = segment(b"x" * 27, 0x700)
    endpoint.receive(frames[0])
    endpoint.poll()
    clock.advance(1)
    with pytest.raises(IsoTpTimeoutError):
        endpoint.poll()
    endpoint.receive(frames[0])
    endpoint.poll()
    endpoint.receive(frames[1])
    with pytest.raises(IsoTpError, match="secuencia"):
        endpoint.receive(frames[1])
    assert not endpoint.receiver.receiving


def test_receiver_rejects_mixed_ids_truncated_cf_and_resets_on_sf() -> None:
    frames = segment(b"x" * 20, 0x700)
    with pytest.raises(IsoTpError, match="dirección"):
        reassemble([frames[0], CanFrame(0x701, frames[1].data), frames[2]])
    with pytest.raises(IsoTpError, match="truncado"):
        reassemble([frames[0], CanFrame(0x700, b"\x21")])
    receiver = IsoTpReceiver()
    receiver.push(frames[0])
    assert receiver.push(CanFrame(0x700, b"\x02ok")) == b"ok"
    with pytest.raises(IsoTpError, match="inesperado"):
        receiver.push(frames[1])


@pytest.mark.parametrize(
    "timeout,waits,maximum", [(0, 3, 4095), (1, -1, 4095), (1, 3, 7), (1, 3, 4096)]
)
def test_invalid_transport_configuration(
    timeout: float, waits: int, maximum: int
) -> None:
    with pytest.raises(ValueError):
        IsoTpTransport(
            0x700, 0x701, timeout=timeout, max_wait_frames=waits, max_payload=maximum
        )


def test_pump_reads_and_writes_explicit_bus() -> None:
    class Port:
        def __init__(self) -> None:
            self.incoming: deque[CanFrame] = deque()
            self.sent: list[CanFrame] = []

        def recv(self, timeout: float | None = None) -> CanFrame | None:
            return self.incoming.popleft() if self.incoming else None

        def send(self, frame: CanFrame) -> None:
            self.sent.append(frame)

    port = Port()
    endpoint = IsoTpTransport(0x700, 0x701)
    endpoint.send(b"x" * 20)
    assert endpoint.pump(port) == []
    assert len(port.sent) == 1
    port.incoming.append(flow_control(0x701))
    assert endpoint.pump(port) == []
    assert len(port.sent) == 3
    port.incoming.append(segment(b"ok", 0x701)[0])
    assert endpoint.pump(port) == [b"ok"]


@pytest.mark.parametrize("length", [0, 7, 8, 4095])
def test_payload_boundaries(length: int) -> None:
    sender = IsoTpTransport(0x700, 0x701, clock=VirtualClock())
    receiver = IsoTpTransport(0x701, 0x700, clock=VirtualClock(), block_size=1)
    expected = bytes(index % 256 for index in range(length))
    sender.send(expected)
    result: bytes | None = None
    for _ in range(600):
        for frame in sender.poll():
            result = receiver.receive(frame)
        for frame in receiver.poll():
            sender.receive(frame)
        if result is not None:
            break
    assert result == expected


def test_reassemble_rejects_incomplete_extra_and_restarted_messages() -> None:
    frames = segment(b"x" * 20, 0x700)
    with pytest.raises(IsoTpError, match="incompleta"):
        reassemble(frames[:-1])
    with pytest.raises(IsoTpError, match="posteriores"):
        reassemble(frames + [CanFrame(0x700, b"\x23extra")])
    with pytest.raises(IsoTpError, match="Consecutive"):
        reassemble([frames[0], CanFrame(0x700, b"\x01x")])


def test_receive_timeout_is_checked_when_next_frame_arrives() -> None:
    clock = VirtualClock()
    endpoint = IsoTpTransport(0x701, 0x700, clock=clock)
    frames = segment(b"x" * 20, 0x700)
    endpoint.receive(frames[0])
    endpoint.poll()
    clock.advance(1)
    with pytest.raises(IsoTpTimeoutError):
        endpoint.receive(frames[1])
    assert endpoint.poll() == []


def test_protocol_clock_and_reset_clear_pending_transfers() -> None:
    clock = VirtualClock()
    ecu = ECU("ECM", 0x7E0, 0x7E8, vin="ECUSDK00000000001")
    ecu.set_protocol_clock(clock)
    request = CanFrame(0x7E0, bytes.fromhex("02 09 02"))
    assert len(ecu.handle_frames(request) or []) == 1
    clock.advance(1)
    assert ecu.handle_frames(flow_control(0x7E0)) is None
    assert len(ecu.handle_frames(request) or []) == 1
    ecu.reset()
    assert ecu.handle_frames(flow_control(0x7E0)) is None
    assert len(ecu.handle_frames(request) or []) == 1
