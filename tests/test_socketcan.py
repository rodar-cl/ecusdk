# pyright: reportPrivateUsage=false

from pathlib import Path

import pytest

import ecusdk.can as can
from ecusdk import AdapterError, CanFrame, SocketCanBus


class FakeSocket:
    def __init__(self) -> None:
        self.bound: tuple[str, ...] | None = None
        self.sent: list[bytes] = []
        self.incoming = b""
        self.timeout: float | None = None
        self.closed = False

    def bind(self, address: tuple[str, ...]) -> None:
        self.bound = address

    def close(self) -> None:
        self.closed = True

    def recv(self, size: int) -> bytes:
        del size
        return self.incoming

    def sendall(self, data: bytes) -> None:
        self.sent.append(data)

    def settimeout(self, timeout: float | None) -> None:
        self.timeout = timeout


def test_socketcan_round_trip_uses_can_raw_layout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeSocket()
    monkeypatch.setattr(can.socket, "AF_CAN", 29, raising=False)
    monkeypatch.setattr(can.socket, "CAN_RAW", 1, raising=False)
    bus = SocketCanBus("can0", socket_factory=lambda _family, _kind, _protocol: fake)

    frame = CanFrame(0x123, b"abc")
    bus.send(frame)
    fake.incoming = fake.sent[0]

    assert fake.bound == ("can0",)
    assert bus.recv(timeout=0.25) == frame
    assert fake.timeout == 0.25
    bus.close()
    assert fake.closed


def test_socketcan_preserves_extended_identifier() -> None:
    frame = CanFrame(0x1234567, b"x", is_extended_id=True)
    # pyright: ignore[reportPrivateUsage]
    assert SocketCanBus._unpack(SocketCanBus._pack(frame)) == frame


def test_socketcan_rejects_truncated_frame() -> None:
    with pytest.raises(AdapterError):
        # pyright: ignore[reportPrivateUsage]
        SocketCanBus._unpack(b"short")


def test_socketcan_requires_explicit_physical_configuration(tmp_path: Path) -> None:
    from ecusdk.config import load_vehicle

    path = tmp_path / "vehicle.toml"
    path.write_text(
        '[vehicle]\nname = "test"\n'
        '[buses.physical]\ntype = "socketcan"\ninterface = "can0"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="physical"):
        load_vehicle(path)
