import socket

import pytest

from ecusdk.cli import main


def test_cli_runs_obd_request(capsys: object) -> None:
    result = main(["run", "examples/demo-car.toml", "--request", "01 0C"])
    assert result == 0
    output = getattr(capsys, "readouterr")().out
    assert "demo-car: running" in output
    assert "04 41 0C 0D 48" in output


def test_run_fails_when_control_port_is_busy(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        argv = ["run", "examples/demo-car.toml", "--port", "0"]
        assert main([*argv, "--control-port", str(port)]) == 1
    assert f"no se pudo abrir 127.0.0.1:{port}" in capsys.readouterr().out
