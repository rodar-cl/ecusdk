from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ecusdk import (
    ECU,
    CanFrame,
    ConfigurationError,
    DiagnosticSession,
    DidSignal,
    Elm327Emulator,
    Nrc,
    UdsError,
    UdsServer,
    Vehicle,
    VehicleShell,
    VirtualCanBus,
    VirtualClock,
    load_vehicle,
)
from ecusdk.uds import S3_SERVER, negative_response

VIN = "ECUSDK00000000001"


def server(dtcs: list[str] | None = None) -> tuple[UdsServer, list[int]]:
    resets: list[int] = []
    uds = UdsServer({"rpm": 850.0}, lambda: dtcs or [], resets.append)
    uds.did(0xF190, VIN)
    return uds, resets


@pytest.mark.parametrize(
    ("request_hex", "response_hex"),
    [
        ("10 01", "50 01 00 32 01 F4"),
        ("10 03", "50 03 00 32 01 F4"),
        ("11 01", "51 01"),
        ("3E 00", "7E 00"),
        ("22 F1 90", "62 F1 90" + VIN.encode().hex()),
        ("19 01 FF", "59 01 09 01 00 02"),
        ("19 02 08", "59 02 09 03 01 00 09 C1 23 00 09"),
        ("19 02 04", "59 02 09"),
        ("19 0A", "59 0A 09 03 01 00 09 C1 23 00 09"),
        ("27 01", "7F 27 11"),
        ("10 04", "7F 10 12"),
        ("10", "7F 10 13"),
        ("11 01 00", "7F 11 13"),
        ("11 05", "7F 11 12"),
        ("3E 01", "7F 3E 12"),
        ("19", "7F 19 13"),
        ("19 03", "7F 19 12"),
        ("19 02", "7F 19 13"),
        ("19 0A 00", "7F 19 13"),
        ("22 F1", "7F 22 13"),
        ("22 12 34", "7F 22 31"),
    ],
)
def test_golden_vectors(request_hex: str, response_hex: str) -> None:
    uds, _ = server(["P0301", "U0123"])
    response = uds.handle(bytes.fromhex(request_hex), now=0)
    assert response == bytes.fromhex(response_hex)


def test_suppress_positive_response_and_reset_callback() -> None:
    uds, resets = server()
    assert uds.handle(b"\x3e\x80", 0) is None
    assert uds.handle(b"\x10\x83", 0) is None
    assert uds.session(0) is DiagnosticSession.EXTENDED
    assert uds.handle(b"\x11\x83", 0) is None
    assert resets == [0x03] and uds.session(0) is DiagnosticSession.DEFAULT
    # Una petición inválida con el bit de supresión sí responde negativamente.
    assert uds.handle(b"\x10\x84", 0) == negative_response(0x10, Nrc(0x12))
    with pytest.raises(UdsError):
        uds.handle(b"", 0)


def test_s3_timeout_and_tester_present_keep_session() -> None:
    uds, _ = server()
    uds.handle(b"\x10\x02", 0)
    uds.handle(b"\x3e\x80", S3_SERVER)
    assert uds.session(S3_SERVER * 2) is DiagnosticSession.PROGRAMMING
    assert uds.session(S3_SERVER * 3 + 0.1) is DiagnosticSession.DEFAULT
    uds.handle(b"\x10\x03", 0)
    uds.reset()
    assert uds.session(0) is DiagnosticSession.DEFAULT


def test_dids_bytes_signals_and_limits() -> None:
    signals = {"rpm": 850.0, "temp": -10.0}
    uds = UdsServer(signals, list)
    uds.did(0x0100, b"\x01\x02")
    uds.did(0x0101, DidSignal("rpm", length=2, scale=4))
    uds.did(0x0102, DidSignal("temp", length=1, offset=-40))
    response = uds.handle(bytes.fromhex("22 01 00 01 01 01 02 99 99"), 0)
    assert response == bytes.fromhex("62 0100 0102 0101 0D48 0102 1E")
    signals["rpm"] = 1e9
    assert uds.handle(bytes.fromhex("22 01 01"), 0) == bytes.fromhex("7F 22 22")
    uds.did(0xF1A0, bytes(4095))
    assert uds.handle(bytes.fromhex("22 F1 A0"), 0) == bytes.fromhex("7F 22 14")
    assert set(uds.dids()) == {0x0100, 0x0101, 0x0102, 0xF1A0}
    for identifier, value in (
        (0x10000, "x"),
        (0x0001, "ñ"),
        (0x0001, DidSignal("missing")),
        (0x0001, 3),
    ):
        with pytest.raises(UdsError):
            uds.did(identifier, value)  # type: ignore[arg-type]  # valores inválidos
    for bad in (
        {"signal": ""},
        {"signal": "rpm", "length": 9},
        {"signal": "rpm", "scale": 0},
    ):
        with pytest.raises(UdsError):
            DidSignal(**bad)  # type: ignore[arg-type]  # parámetros inválidos


@given(st.binary(min_size=1, max_size=64))
def test_any_request_gets_well_formed_answer(request: bytes) -> None:
    uds, _ = server(["P0301"])
    response = uds.handle(request, 0)
    if response is None:
        assert request[0] in (0x10, 0x11, 0x3E) and request[1] & 0x80
    elif response[0] == 0x7F:
        assert response[1] == request[0] and len(response) == 3
        assert response[2] in {nrc.value for nrc in Nrc}
    else:
        assert response[0] == request[0] + 0x40


def make_vehicle() -> tuple[Vehicle, VirtualClock, ECU]:
    clock = VirtualClock()
    ecu = ECU("ecm", 0x7E0, 0x7E8, {"rpm": 850}, vin=VIN)
    vehicle = Vehicle("v", {"bus": VirtualCanBus()}, [ecu], clock)
    vehicle.start()
    return vehicle, clock, ecu


def uds_request(vehicle: Vehicle, payload: str) -> bytes | None:
    data = bytes.fromhex(payload)
    return vehicle.exchange_isotp("bus", CanFrame(0x7E0, bytes((len(data),)) + data))


def test_vehicle_uds_over_isotp() -> None:
    vehicle, clock, ecu = make_vehicle()
    assert uds_request(vehicle, "22 F1 90") == b"\x62\xf1\x90" + VIN.encode()
    assert uds_request(vehicle, "10 03") == bytes.fromhex("50 03 00 32 01 F4")
    assert ecu.uds.session(clock.now()) is DiagnosticSession.EXTENDED
    clock.advance(S3_SERVER + 1)
    assert ecu.uds.session(clock.now()) is DiagnosticSession.DEFAULT
    ecu.dtcs.add("P0301")
    assert uds_request(vehicle, "19 02 FF") == bytes.fromhex("59 02 09 03 01 00 09")
    ecu.set_signal("rpm", 3000)
    assert uds_request(vehicle, "11 03") == b"\x51\x03"
    assert ecu.get_signal("rpm") == 3000
    assert uds_request(vehicle, "11 01") == b"\x51\x01"
    assert ecu.get_signal("rpm") == 850
    assert uds_request(vehicle, "3E 80") is None
    assert uds_request(vehicle, "01 0C") == bytes.fromhex("41 0C 0D 48")
    assert ECU("x", 1, 2).uds.dids() == {}


def test_toml_dids(tmp_path: Path) -> None:
    path = tmp_path / "car.toml"
    base = (
        '[vehicle]\nname = "v"\nvin = "' + VIN + '"\n[buses.can]\ntype = "can"\n'
        "[ecus.ecm.can]\nrequest_id = 0x7E0\nresponse_id = 0x7E8\n"
        "[ecus.ecm.signals.rpm]\ninitial = 850\n"
    )
    path.write_text(
        base + '[ecus.ecm.uds.dids]\n"0xF18C" = "SN-0001"\n"0x0100" = [1, 2]\n'
        '"0x0101" = { signal = "rpm", length = 2, scale = 4 }\n',
        encoding="utf-8",
    )
    ecu = load_vehicle(path).ecu("ecm")
    assert ecu.uds.dids() == {
        0xF190: VIN,
        0xF18C: "SN-0001",
        0x0100: b"\x01\x02",
        0x0101: DidSignal("rpm", 2, 4),
    }
    for dids in (
        '"zz" = "x"',
        '"0x1" = [300]',
        '"0x1" = { signal = "missing" }',
        '"0x1" = { signal = "rpm", bogus = 1 }',
        '"0x1" = 5',
    ):
        path.write_text(base + "[ecus.ecm.uds.dids]\n" + dids + "\n", encoding="utf-8")
        with pytest.raises(ConfigurationError, match="DID"):
            load_vehicle(path)


def test_elm327_and_shell_send_uds() -> None:
    vehicle, _, _ = make_vehicle()
    elm = Elm327Emulator(vehicle)
    vin = VIN.encode()
    assert elm.command("22F190") == (
        f"014\r0: 62 F1 90 {vin[:3].hex(' ').upper()}"
        f"\r1: {vin[3:10].hex(' ').upper()}\r2: {vin[10:].hex(' ').upper()}\r\r>"
    )
    assert elm.command("1003") == "50 03 00 32 01 F4\r\r>"
    assert elm.command("3E80") == "NO DATA\r\r>"
    assert elm.command("2701") == "7F 27 11\r\r>"
    shell = VehicleShell(vehicle)
    assert shell.execute("request ECM 3E 00") == "7E 00"
    assert shell.execute("request ecm 3E80") == "NO DATA"
    assert "did 0xF190" in shell.execute("inspect")
    assert shell.execute("request ecm").startswith("ERROR")
    assert shell.execute("request ecm zz").startswith("ERROR")
