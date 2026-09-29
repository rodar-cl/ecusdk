from ecusdk.cli import main


def test_cli_runs_obd_request(capsys: object) -> None:
    result = main(["run", "examples/demo-car.toml", "--request", "01 0C"])
    assert result == 0
    output = getattr(capsys, "readouterr")().out
    assert "demo-car: running" in output
    assert "04 41 0C 0D 48" in output
