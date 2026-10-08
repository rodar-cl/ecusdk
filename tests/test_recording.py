import io
import json
import signal
from pathlib import Path

import pytest

from ecusdk import (
    ECU,
    CanFrame,
    Recorder,
    RecordingError,
    Scenario,
    ScenarioEvent,
    TcpServer,
    Vehicle,
    VirtualCanBus,
    VirtualClock,
    read_log,
    replay,
)
from ecusdk.cli import main
from ecusdk.recording import Record, format_record

VIN = "ECUSDK00000000001"


def make_vehicle() -> tuple[Vehicle, VirtualClock, ECU]:
    clock = VirtualClock()
    ecu = ECU("ecm", 0x7E0, 0x7E8, {"rpm": 850}, vin=VIN)
    scenario = Scenario([ScenarioEvent(1.5, "ecm", "set", "rpm", 2000)])
    vehicle = Vehicle("v", {"can": VirtualCanBus()}, [ecu], clock, scenario)
    return vehicle, clock, ecu


def request(vehicle: Vehicle, payload: str) -> bytes | None:
    data = bytes.fromhex(payload)
    frame = CanFrame(0x7E0, bytes((len(data),)) + data)
    return vehicle.exchange_isotp("can", frame)


def session(target: Path | io.StringIO) -> None:
    vehicle, clock, ecu = make_vehicle()
    with Recorder(vehicle, target):
        vehicle.start()
        clock.advance(0.5)
        request(vehicle, "01 0C")
        ecu.dtcs.add("P0301")
        vehicle.inject_fault("timeout", "ecm", duration=0.2)
        clock.advance(0.5)
        request(vehicle, "22 F1 90")
        request(vehicle, "27 01")
        clock.advance(0.5)
        vehicle.tick()
        vehicle.pause()
        vehicle.resume()
        ecu.dtcs.remove("P0301")
        clock.advance(0.5)
        vehicle.stop()


def test_recording_captures_every_record_type(tmp_path: Path) -> None:
    path = tmp_path / "s.eculog"
    session(path)
    log = read_log(path)
    assert log.header["format"] == "eculog" and log.header["version"] == 1
    assert log.header["ecus"][0]["request_id"] == 0x7E0
    kinds = {record["type"] for record in log.records}
    assert kinds == {
        "lifecycle",
        "state",
        "frame",
        "diag",
        "signal",
        "dtc",
        "fault",
        "scenario",
    }
    diags = [
        (record["direction"], record["protocol"], record["data"])
        for record in log.records
        if record["type"] == "diag"
    ]
    assert diags == [
        ("request", "obd", "010c"),
        ("response", "obd", "410c0d48"),
        ("request", "uds", "22f190"),
        ("response", "uds", "62f190" + VIN.encode().hex()),
        ("request", "uds", "2701"),
        ("response", "uds", "7f2711"),
    ]
    faults = [r for r in log.records if r["type"] == "fault"]
    assert [(r["action"], r["t"]) for r in faults] == [("start", 0.5), ("end", 0.7)]
    assert faults[0]["until"] == 0.7
    states = [r["state"] for r in log.records if r["type"] == "lifecycle"]
    assert states == ["stopped", "running", "paused", "running", "stopped"]
    assert log.state_at(0) == {
        "ecm": {"signals": {"rpm": 850}, "dtcs": [], "faults": []}
    }
    assert log.state_at(0.6)["ecm"]["dtcs"] == ["P0301"]
    assert log.state_at(0.6)["ecm"]["faults"] == ["timeout"]
    assert log.state_at(1.5)["ecm"] == {
        "signals": {"rpm": 2000},
        "dtcs": ["P0301"],
        "faults": [],
    }
    assert log.state_at(10)["ecm"]["dtcs"] == []
    frames = list(log.frames("can"))
    assert frames[0][2] == CanFrame(0x7E0, bytes.fromhex("02 01 0C"), False, 0.5)
    assert list(log.frames("other")) == []
    lines = [format_record(record) for record in log.records]
    assert any("ecm -> uds 22 F1 90" in line for line in lines)
    assert any("can 7E8 [5] 04 41 0C 0D 48" in line for line in lines)
    assert any("ecm.rpm = 2000" in line for line in lines)


def test_recording_is_deterministic_and_stream_stays_open() -> None:
    first, second = io.StringIO(), io.StringIO()
    session(first)
    session(second)
    assert first.getvalue() == second.getvalue()
    assert not first.closed


def test_replay_preserves_order_and_relative_time(tmp_path: Path) -> None:
    path = tmp_path / "s.eculog"
    session(path)
    log = read_log(path)
    clock = VirtualClock(100)
    seen: list[tuple[float, Record]] = []
    replay(log, lambda record: seen.append((clock.now(), record)), clock=clock)
    assert [record for _, record in seen] == log.records
    assert all(at == pytest.approx(100 + r["t"]) for at, r in seen)
    clock = VirtualClock()
    replay(log, lambda record: None, clock=clock, speed=2)
    assert clock.now() == pytest.approx(log.records[-1]["t"] / 2)
    clock = VirtualClock()
    replay(log, lambda record: None, clock=clock, speed=0)
    assert clock.now() == 0
    with pytest.raises(ValueError):
        replay(log, lambda record: None, speed=-1)
    bus = VirtualCanBus()
    tap = bus.connect()
    replay(
        log,
        lambda r: (
            bus.send(CanFrame(r["id"], bytes.fromhex(r["data"])))
            if r["type"] == "frame"
            else None
        ),
        speed=0,
    )
    replayed = [frame for frame in iter(lambda: tap.recv(timeout=0), None)]
    assert [f.data for f in replayed] == [f.data for _, _, f in log.frames()]


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("", "vacío"),
        ("{bad\n", "JSON"),
        ("[1]\n", "inválido"),
        ('{"format": "pcap"}\n', "eculog"),
        ('{"format": "eculog", "version": 2}\n', "versión"),
        ('{"format": "eculog", "version": 1}\n{"t": 0, "type": "x"}\n', "línea 2"),
        (
            '{"format": "eculog", "version": 1}\n{"t": "0", "type": "frame"}\n',
            "línea 2",
        ),
        (
            '{"format": "eculog", "version": 1}\n'
            '{"t": 1, "type": "lifecycle"}\n{"t": 0.5, "type": "lifecycle"}\n',
            "decreciente",
        ),
    ],
)
def test_invalid_logs(tmp_path: Path, content: str, message: str) -> None:
    path = tmp_path / "bad.eculog"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(RecordingError, match=message):
        read_log(path)


def test_recorder_lifecycle_guards() -> None:
    vehicle, _, _ = make_vehicle()
    recorder = Recorder(vehicle, io.StringIO())
    with pytest.raises(RecordingError):
        Recorder(vehicle, io.StringIO())
    bus = vehicle.buses["can"]
    assert isinstance(bus, VirtualCanBus)
    bus.close()
    vehicle.tick()
    recorder.capture()  # el tap cerrado se descarta sin error
    recorder.close()
    recorder.close()
    recorder.capture()
    assert vehicle.recorder is None


def test_cli_record_and_replay(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "cli.eculog"
    args = ["run", "examples/demo-car.toml", "--request", "01 0C"]
    assert main([*args, "--record", str(path)]) == 0
    capsys.readouterr()
    assert main(["replay", str(path), "--fast", "--type", "diag"]) == 0
    output = capsys.readouterr().out.splitlines()
    assert [line.split()[1] for line in output] == ["diag", "diag"]
    assert main(["replay", str(path), "--fast", "--bus", "other"]) == 0
    assert "frame" not in capsys.readouterr().out
    sent: list[CanFrame] = []

    class FakeSocketCan:
        def __init__(self, interface: str) -> None:
            assert interface == "vcan0"

        def send(self, frame: CanFrame) -> None:
            sent.append(frame)

        def close(self) -> None:
            sent.append(CanFrame(0, b"closed"))

    monkeypatch.setattr("ecusdk.cli.SocketCanBus", FakeSocketCan)
    assert main(["replay", str(path), "--speed", "1000", "--socketcan", "vcan0"]) == 0
    assert [frame.data for frame in sent] == [
        bytes.fromhex("02 01 0C"),
        bytes.fromhex("04 41 0C 0D 48"),
        b"closed",
    ]

    def interrupt(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("ecusdk.cli.replay", interrupt)
    assert main(["replay", str(path)]) == 0
    assert main(["replay", str(tmp_path / "missing.eculog")]) == 1
    lines = path.read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0])["vehicle"] == "demo-car"


def test_sigterm_stops_run_and_restores_handler(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    before = signal.getsignal(signal.SIGTERM)

    def terminate(self: TcpServer) -> None:
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        handler(signal.SIGTERM, None)

    monkeypatch.setattr(TcpServer, "serve_forever", terminate)
    path = tmp_path / "term.eculog"
    args = ["run", "examples/demo-car.toml", "--port", "0", "--control-port", "0"]
    assert main([*args, "--record", str(path)]) == 0
    assert signal.getsignal(signal.SIGTERM) is before
    states = [r["state"] for r in read_log(path).records if r["type"] == "lifecycle"]
    assert states == ["stopped", "running", "stopped"]
