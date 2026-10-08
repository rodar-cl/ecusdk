import asyncio
import contextlib
import enum
import sys
import time
import types
from typing import Any

import pytest

from ecusdk.ble import RX_UUID, SERVICE_UUID, TX_UUID, chunks, serve_ble
from ecusdk.config import load_vehicle
from ecusdk.elm327 import Elm327Emulator
from ecusdk.errors import AdapterError


class Props(enum.Flag):
    read = 0x02
    write_without_response = 0x04
    write = 0x08
    notify = 0x10


class Perms(enum.Flag):
    readable = 0x1
    writeable = 0x2


class FakeServer:
    """Sustituto de ``bless.BlessServer`` que registra las notificaciones."""

    last: "FakeServer | None" = None

    def __init__(self, name: str, loop: object) -> None:
        self.name = name
        self.chars: dict[str, Any] = {}
        self.sent: list[bytes] = []
        self.busy = 1  # la primera notificación encuentra la cola llena
        self.started = False
        self.stopped = False
        self.write_request_func: Any = None
        self.read_request_func: Any = None
        FakeServer.last = self

    async def add_new_service(self, uuid: str) -> None:
        self.service = uuid

    async def add_new_characteristic(
        self, service: str, uuid: str, props: Props, value: object, perms: Perms
    ) -> None:
        self.chars[uuid] = types.SimpleNamespace(props=props, perms=perms, value=value)

    def get_characteristic(self, uuid: str) -> Any:
        return self.chars[uuid]

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    def update_value(self, service: str, uuid: str) -> bool:
        if self.busy:
            self.busy -= 1
            return False
        self.sent.append(bytes(self.chars[uuid].value))
        return True


def test_chunks() -> None:
    assert chunks(b"abcde", 2) == [b"ab", b"cd", b"e"]
    with pytest.raises(ValueError):
        chunks(b"a", 0)


def test_ble_serves_elm327_in_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    bless = types.SimpleNamespace(
        BlessServer=FakeServer,
        GATTCharacteristicProperties=Props,
        GATTAttributePermissions=Perms,
    )
    monkeypatch.setitem(sys.modules, "bless", bless)
    vehicle = load_vehicle("examples/demo-car.toml")
    vehicle.start()

    async def scenario() -> FakeServer:
        task = asyncio.create_task(serve_ble(Elm327Emulator(vehicle), chunk=8))
        deadline = time.monotonic() + 2
        while not (FakeServer.last and FakeServer.last.started):
            assert time.monotonic() < deadline
            await asyncio.sleep(0.001)
        server = FakeServer.last
        rx = server.chars[RX_UUID]
        server.write_request_func(rx, bytearray(b"ATE0\r09"))
        server.write_request_func(rx, bytearray(b"02\r"))
        while b"".join(server.sent).count(b">") < 2:
            assert time.monotonic() < deadline
            await asyncio.sleep(0.001)
        assert server.read_request_func(server.chars[TX_UUID]) == server.sent[-1]
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        return server

    server = asyncio.run(scenario())
    assert server.name == "OBDII ecusdk"
    assert server.service == SERVICE_UUID
    assert server.chars[RX_UUID].props == Props.write | Props.write_without_response
    assert server.chars[TX_UUID].props == Props.read | Props.notify
    assert all(len(piece) <= 8 for piece in server.sent)
    joined = b"".join(server.sent)
    assert b"OK" in joined
    assert b"45 43 55 53 44 4B" in joined  # VIN "ECUSDK..." por 0902
    assert server.stopped


def test_ble_without_extra_explains_install(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "bless", None)
    vehicle = load_vehicle("examples/demo-car.toml")
    with pytest.raises(AdapterError, match=r"ecusdk\[ble\]"):
        asyncio.run(serve_ble(Elm327Emulator(vehicle)))
    with pytest.raises(ValueError):
        asyncio.run(serve_ble(Elm327Emulator(vehicle), chunk=0))


def test_cli_ble_without_extra_fails_cleanly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from ecusdk.cli import main

    monkeypatch.setitem(sys.modules, "bless", None)
    argv = ["run", "examples/demo-car.toml", "--ble", "--port", "0"]
    assert main([*argv, "--control-port", "0"]) == 1
    assert "ecusdk[ble]" in capsys.readouterr().out
