from typing import cast

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ecusdk.can import CanFrame
from ecusdk.clock import VirtualClock
from ecusdk.config import load_vehicle
from ecusdk.errors import IsoTpError, IsoTpTimeoutError
from ecusdk.isotp import IsoTpReceiver, flow_control, reassemble, segment
from ecusdk.obd import DtcStore, decode_dtc, encode_dtc


@given(st.binary(min_size=0, max_size=4095))
def test_isotp_round_trip(payload: bytes) -> None:
    assert reassemble(segment(payload, 0x700)) == payload


def test_isotp_rejects_wrong_sequence() -> None:
    frames = segment(b"a" * 20, 0x700)
    frames[1] = CanFrame(0x700, bytes((0x22,)) + frames[1].data[1:])
    with pytest.raises(IsoTpError):
        reassemble(frames)


def test_flow_control() -> None:
    assert flow_control(0x701, 8, 20).data == bytes.fromhex("30 08 14")


def test_isotp_rejects_malformed_first_frame() -> None:
    frame = CanFrame(0x700, bytes.fromhex("10 07") + b"x" * 6)
    with pytest.raises(IsoTpError):
        reassemble([frame])
    with pytest.raises(IsoTpError):
        IsoTpReceiver().push(frame)


def test_can_frame_copies_mutable_data() -> None:
    data = bytearray(b"x")
    frame = CanFrame(0x700, cast(bytes, data))
    data[0] = ord("y")
    assert frame.data == b"x"


def test_isotp_receiver_times_out_with_virtual_clock() -> None:
    clock = VirtualClock()
    receiver = IsoTpReceiver(timeout=2, clock=clock)
    receiver.push(segment(b"a" * 20, 0x700)[0])
    clock.advance(2)
    with pytest.raises(IsoTpTimeoutError):
        receiver.check_timeout()


def test_isotp_receiver_accepts_consecutive_frame_before_deadline() -> None:
    clock = VirtualClock()
    receiver = IsoTpReceiver(timeout=2, clock=clock)
    frames = segment(b"a" * 20, 0x700)
    assert receiver.push(frames[0]) is None
    clock.advance(1)
    assert receiver.push(frames[1]) is None
    assert receiver.push(frames[2]) == b"a" * 20


def test_dtc_golden_vectors() -> None:
    vectors = {
        "P0301": "03 01",
        "P03AF": "03 AF",
        "P0420": "04 20",
        "B1234": "52 34",
        "C0035": "80 35",
        "U0100": "C1 00",
    }
    for code, encoded in vectors.items():
        assert encode_dtc(code) == bytes.fromhex(encoded)
        assert decode_dtc(bytes.fromhex(encoded)) == code


def test_dtc_rejects_first_digit_outside_two_bit_range() -> None:
    for code in ("P4301", "B9301", "CA301", "UF301"):
        with pytest.raises(ValueError):
            encode_dtc(code)


def test_dtc_round_trip() -> None:
    assert encode_dtc("p03af") == bytes.fromhex("03 AF")
    assert decode_dtc(encode_dtc("P0301")) == "P0301"
    store = DtcStore()
    store.add("P0301")
    assert store.list() == ["P0301"]
    store.clear()
    assert store.list() == []


def test_obd_modes_03_04_and_vin() -> None:
    vehicle = load_vehicle("examples/demo-car.toml")
    ecu = vehicle.ecus[0]
    ecu.dtcs.add("P0301")
    assert ecu.obd_payload(bytes.fromhex("03")) == bytes.fromhex("43 03 01")
    assert ecu.obd_payload(bytes.fromhex("04")) == bytes.fromhex("44")
    assert ecu.dtcs.list() == []
    assert ecu.obd_payload(bytes.fromhex("09 02")) == (
        bytes.fromhex("49 02 01") + b"ECUSDK00000000001"
    )
