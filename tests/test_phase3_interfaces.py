import socket
import threading
import time
from pathlib import Path

import pytest

from ecusdk import (
    ECU,
    PID_CATALOG,
    CanError,
    CanFrame,
    ConfigurationError,
    ECUSDKError,
    Elm327Emulator,
    ObdError,
    PidCodec,
    SimulationError,
    TcpServer,
    Vehicle,
    VehicleShell,
    VirtualCanBus,
    VirtualClock,
)
from ecusdk.cli import main
from ecusdk.config import load_vehicle
from ecusdk.shell import parse_duration, send_command

SCENARIO_CAR = "examples/scenario-car.toml"


def codec(identifier: int) -> PidCodec:
    return next(item for item in PID_CATALOG if item.identifier == identifier)


def test_catalog_codecs_offsets_and_percentages() -> None:
    coolant, load, voltage = codec(0x05), codec(0x04), codec(0x42)
    assert coolant.encode(-40) == b"\x00" and coolant.encode(90) == bytes((130,))
    assert coolant.decode(bytes((130,))) == 90
    assert load.encode(100) == b"\xff" and load.decode(b"\xff") == pytest.approx(100)
    assert voltage.encode(14.1) == (14100).to_bytes(2, "big")
    with pytest.raises(ObdError):
        coolant.encode(-41)
    with pytest.raises(ObdError):
        PidCodec(0x99, 1, "x", -50, 10, 1, "x", -40)


def test_supported_pid_bitmaps_follow_bindings() -> None:
    ecu = ECU("ecm", 0x7E0, 0x7E8, {"rpm": 850, "coolant": 90, "volts": 14})
    assert ecu.obd_payload(b"\x01\x00") == bytes.fromhex("41 00 00 18 00 00")
    assert ecu.obd_payload(b"\x01\x20") is None
    ecu.obd.pid(0x05, source="coolant")
    ecu.obd.pid(0x42, source="volts")
    assert ecu.obd_payload(b"\x01\x00") == bytes.fromhex("41 00 08 18 00 01")
    assert ecu.obd_payload(b"\x01\x20") == bytes.fromhex("41 20 00 00 00 01")
    assert ecu.obd_payload(b"\x01\x40") == bytes.fromhex("41 40 40 00 00 00")
    assert ecu.obd_payload(b"\x01\x05") == bytes.fromhex("41 05 82")
    assert ecu.obd.supported() == [0x05, 0x0C, 0x0D, 0x42]


def test_error_hierarchy_keeps_standard_bases() -> None:
    assert issubclass(ConfigurationError, ValueError)
    assert issubclass(SimulationError, ECUSDKError)
    with pytest.raises(CanError):
        CanFrame(0x800, b"")


def test_toml_scenario_generators_and_seed() -> None:
    vehicle = load_vehicle(SCENARIO_CAR)
    clock = VirtualClock()
    vehicle.clock = clock
    for ecu in vehicle.ecus:
        ecu.set_protocol_clock(clock)
    assert vehicle.seed == 7 and vehicle.scenario is not None
    ecm, tcm = vehicle.ecu("ECM"), vehicle.ecu("tcm")
    vehicle.start()
    vehicle.advance(10)
    assert ecm.get_signal("rpm") == pytest.approx(1925)
    assert 13.8 <= ecm.get_signal("battery_voltage") <= 14.4
    vehicle.advance(10.5)
    frozen = ecm.get_signal("rpm")
    vehicle.advance(2)
    assert ecm.get_signal("rpm") == frozen
    vehicle.advance(8)
    assert [fault.kind for fault in vehicle.faults("tcm")] == ["ecu_offline"]
    assert tcm.get_signal("speed") != 0


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ('[vehicle]\nname = "v"\n[ecus.e.can]\nrequest_id = 1\n', "request_id"),
        ("[vehicle]\n", "name"),
        ("not toml [", "TOML"),
        (
            '[vehicle]\nname = "v"\n[ecus.e.can]\nrequest_id = 1\nresponse_id = 2\n'
            '[ecus.e.signals.rpm]\ngenerator = { type = "warp" }\n',
            "generador",
        ),
        (
            '[vehicle]\nname = "v"\n[ecus.e.can]\nrequest_id = 1\nresponse_id = 2\n'
            '[ecus.e.obd]\n"01:05" = "missing"\n',
            "señal",
        ),
        (
            '[vehicle]\nname = "v"\n[ecus.e.can]\nrequest_id = 1\nresponse_id = 2\n'
            '[scenario]\nevents = [{ at = 0, ecu = "x", action = "timeout" }]\n',
            "desconocida",
        ),
        (
            '[vehicle]\nname = "v"\n[scenario]\n'
            'events = [{ at = 0, ecu = "x", action = "warp" }]\n',
            "escenario",
        ),
        ('[vehicle]\nname = "v"\n[scenario]\nevents = 3\n', "lista"),
        ('[vehicle]\nname = "v"\nseed = "x"\n', "seed"),
    ],
)
def test_invalid_configuration(tmp_path: Path, body: str, message: str) -> None:
    path = tmp_path / "bad.toml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(ConfigurationError, match=message):
        load_vehicle(path)


def two_ecu_vehicle() -> tuple[Vehicle, VirtualClock]:
    clock = VirtualClock()
    ecm = ECU("ecm", 0x7E0, 0x7E8, {"rpm": 850, "speed": 10}, vin="ECUSDK00000000001")
    tcm = ECU("tcm", 0x7E1, 0x7E9, {"speed": 20})
    tcm.obd.pid(0x0C, source="speed")
    vehicle = Vehicle("v", {"bus": VirtualCanBus()}, [ecm, tcm], clock)
    vehicle.start()
    return vehicle, clock


def test_elm327_functional_and_physical_addressing() -> None:
    vehicle, clock = two_ecu_vehicle()
    elm = Elm327Emulator(vehicle)
    assert elm.command("ATH1") == "OK\r\r>"
    assert elm.command("010D") == "7E8  41 0D 0A\r7E9  41 0D 14\r\r>"
    assert elm.command("ATSH7E1") == "OK\r\r>"
    assert elm.command("01 0D") == "7E9  41 0D 14\r\r>"
    assert elm.command("ATS0") == "OK\r\r>"
    assert elm.command("010D") == "7E9410D14\r\r>"
    assert elm.command("ATD") == "OK\r\r>"
    assert elm.command("0105") == "NO DATA\r\r>"
    assert clock.now() == pytest.approx(0.4)  # espera ATST por cada ECU
    assert elm.command("0100").startswith("41 00 00 18 00 00\r")
    for command in ("ATL0", "ATST32", "ATAT1", "ATI"):
        assert elm.command(command) != "?\r\r>"
    for command in ("0G", "ATSHXYZ", "FF00", "ATFOO"):
        assert elm.command(command) == "?\r\r>"
    vehicle.inject_fault("malformed_frame", "ecm")
    vehicle.inject_fault("ecu_offline", "tcm")
    assert elm.command("010C") == "CAN ERROR\r\r>"
    vehicle.stop()
    assert elm.command("010C") == "NO DATA\r\r>"


def test_shell_commands() -> None:
    vehicle, clock = two_ecu_vehicle()
    shell = VehicleShell(vehicle)
    assert shell.execute("list ecus") == "ecm\ntcm"
    assert shell.execute("set ECM.rpm 3500") == "OK"
    assert shell.execute("get ecm.rpm") == "3500"
    assert shell.execute("dtc ECM add P0301") == "OK"
    assert shell.execute("dtc ECM list") == "P0301"
    assert shell.execute("fault ECM timeout 500ms") == "OK"
    assert shell.execute("faults").startswith("ecm timeout (0.500s left)")
    assert shell.execute("fault ecm sensor_frozen signal=rpm") == "OK"
    assert shell.execute("fault ecm latency 2s delay=100ms") == "OK"
    assert "fault sensor_frozen" in shell.execute("inspect")
    assert shell.execute("fault ecm clear sensor_frozen") == "OK"
    assert shell.execute("fault ecm clear") == "OK"
    assert shell.execute("faults ecm") == "(none)"
    assert shell.execute("dtc ecm remove P0301") == "OK"
    assert shell.execute("dtc ecm clear") == "OK"
    assert shell.execute("dtc ecm list") == "(none)"
    assert shell.execute("pause") == "paused"
    assert shell.execute("resume") == "running"
    assert shell.execute("reset") == "running"
    assert shell.execute("get ecm.rpm") == "850"
    assert shell.execute("") == ""
    assert "set ECU.signal" in shell.execute("help")
    for bad in (
        "warp",
        "list things",
        "get ecm",
        "get missing.rpm",
        "set ecm.rpm fast",
        "set ecm.rpm inf",
        "dtc ecm add BAD",
        "dtc ecm frob",
        "fault ecm explode",
        "fault ecm timeout soon",
        "fault ecm latency bogus=1",
        "fault ecm sensor_frozen",
    ):
        assert shell.execute(bad).startswith("ERROR"), bad
    assert parse_duration("1.5") == 1.5 and clock.now() == 0


def serve(service: VehicleShell | Elm327Emulator) -> tuple[TcpServer, threading.Thread]:
    server = TcpServer(service, port=0)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    deadline = time.monotonic() + 2
    while server.port == 0 and time.monotonic() < deadline:
        time.sleep(0.001)
    return server, thread


def test_cli_controls_a_running_vehicle(capsys: pytest.CaptureFixture[str]) -> None:
    vehicle, _ = two_ecu_vehicle()
    server, thread = serve(VehicleShell(vehicle))
    port = ["--control-port", str(server.port)]
    try:
        assert main(["set", "ECM.rpm", "3500", *port]) == 0
        assert main(["get", "ECM.rpm", *port]) == 0
        assert main(["dtc", "ECM", "add", "P0301", *port]) == 0
        assert main(["fault", "ECM", "timeout", "500ms", *port]) == 0
        assert main(["faults", *port]) == 0
        assert main(["inspect", *port]) == 0
        assert main(["get", "nope.rpm", *port]) == 1
        assert send_command("127.0.0.1", server.port, "get ecm.rpm") == "3500"
        with socket.create_connection(("127.0.0.1", server.port)) as client:
            client.sendall(b"get ecm.rpm\nlist ecus\n")
            client.shutdown(socket.SHUT_WR)
            data = b"".join(iter(lambda: client.recv(4096), b""))
        assert data == b"3500\necm\ntcm\n"
    finally:
        server.close()
        thread.join(timeout=2)
    output = capsys.readouterr().out
    assert "OK" in output and "3500" in output and "P0301" in output


def test_cli_shell_and_offline_inspect(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["inspect", SCENARIO_CAR]) == 0
    assert "pid 01:42 -> battery_voltage" in capsys.readouterr().out
    vehicle, _ = two_ecu_vehicle()
    server, thread = serve(VehicleShell(vehicle))
    lines = iter(["list ecus", "", "exit"])

    def scripted(prompt: str) -> str:
        return next(lines)

    monkeypatch.setattr("builtins.input", scripted)
    try:
        assert main(["shell", "--control-port", str(server.port)]) == 0
    finally:
        server.close()
        thread.join(timeout=2)
    assert "ecm\ntcm" in capsys.readouterr().out

    def eof(prompt: str) -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    assert main(["shell", "--control-port", "1"]) == 0

    def repeat(prompt: str) -> str:
        return "list ecus"

    monkeypatch.setattr("builtins.input", repeat)
    assert main(["shell", "--control-port", "1"]) == 1
    with pytest.raises(SystemExit):
        main(["get", "ecm.rpm", "--control-port", "1"])


def test_cli_run_serves_elm_and_control(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[str] = []

    def fake_serve(self: TcpServer) -> None:
        started.append(type(self.emulator).__name__)
        if isinstance(self.emulator, Elm327Emulator):
            raise KeyboardInterrupt

    monkeypatch.setattr(TcpServer, "serve_forever", fake_serve)
    assert main(["run", SCENARIO_CAR, "--port", "0", "--control-port", "0"]) == 0
    assert started == ["Elm327Emulator"]
    assert main(["run", SCENARIO_CAR, "--port", "0", "--control-port", "9"]) == 0
    assert sorted(started) == ["Elm327Emulator", "Elm327Emulator", "VehicleShell"]
