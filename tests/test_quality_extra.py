from pathlib import Path

import pytest

from ecusdk.can import SocketCanBus
from ecusdk.cli import main
from ecusdk.errors import AdapterError


class BrokenSocket:
    def bind(self, address: tuple[str, ...]) -> None:
        raise OSError("no interface")

    def close(self) -> None:
        raise OSError("already closed")

    def recv(self, size: int) -> bytes:
        raise OSError("read")

    def sendall(self, data: bytes) -> None:
        raise OSError("write")

    def settimeout(self, timeout: float | None) -> None:
        pass


def test_socketcan_wraps_adapter_errors() -> None:
    with pytest.raises(AdapterError):
        SocketCanBus(
            "can0",
            socket_factory=lambda _family, _kind, _protocol: BrokenSocket(),
        )


def test_cli_request_and_once(capsys: pytest.CaptureFixture[str]) -> None:
    config = str(Path("examples/demo-car.toml"))
    assert main(["run", config, "--request", "01 0C"]) == 0
    assert "41 0C" in capsys.readouterr().out
    assert main(["run", config, "--once"]) == 0
    assert "running" in capsys.readouterr().out
