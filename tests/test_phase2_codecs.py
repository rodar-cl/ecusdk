import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ecusdk import RPM_PID, SPEED_PID, CanFrame, PidCodec
from ecusdk.errors import ObdError
from ecusdk.obd import decode_dtc, encode_dtc


@given(st.sampled_from("PBCU"), st.integers(0, 3), st.integers(0, 4095))
def test_dtc_property(prefix: str, first_digit: int, suffix: int) -> None:
    code = f"{prefix}{first_digit}{suffix:03X}"
    assert decode_dtc(encode_dtc(code)) == code


@given(st.binary(min_size=2, max_size=2))
def test_dtc_all_wire_values_round_trip(data: bytes) -> None:
    assert encode_dtc(decode_dtc(data)) == data


@given(st.integers(0, 65535))
def test_rpm_codec_round_trip(raw: int) -> None:
    value = raw / 4
    assert RPM_PID.decode(RPM_PID.encode(value)) == value


@given(st.integers(0, 255))
def test_speed_codec_round_trip(value: int) -> None:
    assert SPEED_PID.decode(SPEED_PID.encode(value)) == value


@given(
    st.floats(min_value=0, max_value=16383.75, allow_nan=False, allow_infinity=False)
)
def test_rpm_quantization(value: float) -> None:
    assert abs(RPM_PID.decode(RPM_PID.encode(value)) - value) <= 0.125


@pytest.mark.parametrize(
    "codec,value,wire", [(RPM_PID, 850, b"\x0dH"), (SPEED_PID, 100, b"d")]
)
def test_pid_golden_vectors(codec: PidCodec, value: int, wire: bytes) -> None:
    assert codec.encode(value) == wire
    assert codec.decode(wire) == value
    assert codec.unit and codec.source


@pytest.mark.parametrize("value", [-1, math.inf, math.nan, 16384])
def test_pid_rejects_invalid_values(value: float) -> None:
    with pytest.raises(ObdError):
        RPM_PID.encode(value)


def test_pid_rejects_lengths_configuration_and_out_of_range_decode() -> None:
    with pytest.raises(ObdError):
        RPM_PID.decode(b"x")
    with pytest.raises(ObdError):
        PidCodec(256, 1, "x", 0, 255, 1, "x")
    codec = PidCodec(1, 1, "x", 0, 10, 1, "x")
    with pytest.raises(ObdError):
        codec.decode(b"\x0b")


@given(st.booleans(), st.integers(0, 0x1FFFFFFF), st.binary(max_size=8))
def test_can_identifier_validation(
    extended: bool, identifier: int, data: bytes
) -> None:
    limit = 0x1FFFFFFF if extended else 0x7FF
    if identifier > limit:
        with pytest.raises(ValueError):
            CanFrame(identifier, data, is_extended_id=extended)
    else:
        frame = CanFrame(identifier, data, is_extended_id=extended)
        assert frame.arbitration_id == identifier and frame.data == data


@given(st.binary(min_size=9, max_size=64))
def test_can_rejects_oversize_payload(data: bytes) -> None:
    with pytest.raises(ValueError):
        CanFrame(0x700, data)


@pytest.mark.parametrize(
    "minimum,maximum,scale", [(0.1, 10, 1), (0, 10.1, 1), (0, 255, 1e308)]
)
def test_pid_rejects_unrepresentable_range(
    minimum: float, maximum: float, scale: float
) -> None:
    with pytest.raises(ObdError):
        PidCodec(1, 1, "x", minimum, maximum, scale, "x")
