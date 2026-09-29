from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from threading import Event, Thread
from typing import cast

import pytest

from ecusdk.can import CanFilter, CanFrame, VirtualCanBus
from ecusdk.clock import VirtualClock
from ecusdk.errors import AdapterError


def test_nodes_receive_broadcast_independently_and_hub_is_passive_tap() -> None:
    bus = VirtualCanBus()
    first = bus.connect()
    second = bus.connect()
    frame = CanFrame(0x123, b"payload")

    first.send(frame)

    assert first.recv(timeout=0) is None
    assert second.recv(timeout=0) == frame
    assert bus.recv(timeout=0) == frame


def test_node_echo_is_opt_in_and_filter_matches_mask_and_format() -> None:
    bus = VirtualCanBus()
    sender = bus.connect(receive_own_messages=True)
    standard = bus.connect(filters=(CanFilter(0x120, 0x7F0, False),))
    extended = bus.connect(filters=(CanFilter(0x123, 0x7FF, True),))
    sender.send(CanFrame(0x127, b"s"))

    assert sender.recv(timeout=0) == CanFrame(0x127, b"s")
    assert standard.recv(timeout=0) == CanFrame(0x127, b"s")
    assert extended.recv(timeout=0) is None
    sender.send(CanFrame(0x123, b"e", is_extended_id=True))
    assert standard.recv(timeout=0) is None
    assert extended.recv(timeout=0) == CanFrame(0x123, b"e", is_extended_id=True)


def test_filter_replacement_and_empty_filter_accepts_all() -> None:
    bus = VirtualCanBus()
    node = bus.connect(filters=(CanFilter(1, 0x7FF),))
    bus.send(CanFrame(2, b"ignored"))
    assert node.recv(timeout=0) is None
    node.set_filters((CanFilter(2, 0x7FF),))
    bus.send(CanFrame(2, b"accepted"))
    assert node.recv(timeout=0) == CanFrame(2, b"accepted")
    node.set_filters(())
    bus.send(CanFrame(3, b"all"))
    assert node.recv(timeout=0) == CanFrame(3, b"all")


def test_clock_stamps_once_preserves_explicit_timestamp_and_can_be_replaced() -> None:
    clock = VirtualClock(12.5)
    bus = VirtualCanBus(clock=clock)
    node = bus.connect()
    bus.send(CanFrame(1, b"a"))
    stamped = CanFrame(1, b"a", timestamp=12.5)
    assert node.recv(timeout=0) == stamped
    assert bus.recv(timeout=0) == stamped
    explicit = CanFrame(2, b"b", timestamp=9.0)
    bus.send(explicit)
    assert node.recv(timeout=0) == explicit
    assert bus.recv(timeout=0) == explicit
    bus.set_clock(None)
    bus.send(CanFrame(3, b"c"))
    received = node.recv(timeout=0)
    assert received is not None and received.timestamp is None


def test_close_removes_node_and_hub_reset_does_not_reactivate_nodes() -> None:
    bus = VirtualCanBus()
    node = bus.connect()
    node.close()
    node.close()
    with pytest.raises(AdapterError):
        node.recv(timeout=0)
    with pytest.raises(AdapterError):
        node.send(CanFrame(1, b"x"))
    bus.send(CanFrame(1, b"x"))
    assert bus.recv(timeout=0) == CanFrame(1, b"x")

    survivor = bus.connect()
    bus.close()
    bus.close()
    with pytest.raises(AdapterError):
        survivor.recv(timeout=0)
    with pytest.raises(AdapterError):
        bus.recv(timeout=0)
    with pytest.raises(AdapterError):
        bus.send(CanFrame(1, b"x"))
    bus.reset()
    with pytest.raises(AdapterError):
        survivor.send(CanFrame(1, b"x"))
    fresh = bus.connect()
    bus.send(CanFrame(1, b"fresh"))
    assert fresh.recv(timeout=0) == CanFrame(1, b"fresh")


def test_closing_node_unblocks_waiting_receiver() -> None:
    bus = VirtualCanBus()
    node = bus.connect()
    started = Event()

    results: Queue[Exception | CanFrame | None] = Queue()

    def receive() -> None:
        started.set()
        try:
            results.put(node.recv())
        except Exception as error:
            results.put(error)

    waiting = Thread(target=receive, daemon=True)
    waiting.start()
    assert started.wait(timeout=1)
    node.close()
    waiting.join(timeout=1)
    assert not waiting.is_alive(), "node.recv quedó bloqueado después de close()"
    assert isinstance(results.get_nowait(), AdapterError)


@pytest.mark.parametrize("endpoint", ["node", "hub"])
def test_close_unblocks_multiple_receivers(endpoint: str) -> None:
    bus = VirtualCanBus()
    node = bus.connect()
    receiver = node if endpoint == "node" else bus
    ready = [Event(), Event()]
    results: Queue[Exception | CanFrame | None] = Queue()

    def receive(index: int) -> None:
        ready[index].set()
        try:
            results.put(receiver.recv())
        except Exception as error:
            results.put(error)

    waiting = [Thread(target=receive, args=(index,), daemon=True) for index in range(2)]
    for thread in waiting:
        thread.start()
    assert all(event.wait(timeout=1) for event in ready)
    if endpoint == "node":
        node.close()
    else:
        bus.close()
    for thread in waiting:
        thread.join(timeout=1)
        assert not thread.is_alive(), (
            f"{endpoint}.recv quedó bloqueado después de close()"
        )
    assert all(isinstance(results.get_nowait(), AdapterError) for _ in waiting)


def test_reset_disconnects_live_nodes_and_old_blocked_hub_receivers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus = VirtualCanBus()
    old_node = bus.connect()
    waiting_queue = _SignallingQueue()
    # Instrument queue.get to coordinate reset after recv has captured the old queue.
    monkeypatch.setattr(bus, "_frames", waiting_queue)
    results: Queue[Exception | CanFrame | None] = Queue()

    def receive() -> None:
        try:
            results.put(bus.recv())
        except Exception as error:
            results.put(error)

    waiting = Thread(target=receive, daemon=True)
    waiting.start()
    assert waiting_queue.get_started.wait(timeout=1)
    bus.reset()
    waiting.join(timeout=1)
    assert not waiting.is_alive(), "hub.recv quedó bloqueado después de reset()"
    assert isinstance(results.get_nowait(), AdapterError)

    with pytest.raises(AdapterError):
        old_node.recv(timeout=0)
    bus.send(CanFrame(8, b"stale"))
    bus.reset()
    assert bus.recv(timeout=0) is None
    fresh = bus.connect()
    bus.send(CanFrame(9, b"new"))
    assert fresh.recv(timeout=0) == CanFrame(9, b"new")


def test_filter_configuration_validation() -> None:
    with pytest.raises(ValueError):
        CanFilter(-1, 1)
    with pytest.raises(ValueError):
        CanFilter(1, 0x20000000)
    with pytest.raises(ValueError):
        CanFilter(0x800, 0x7FF, False)
    with pytest.raises(ValueError):
        # A cast preserves the intentional runtime-invalid value for validation.
        CanFilter(1, 1, cast(bool | None, 1))
    bus = VirtualCanBus()
    with pytest.raises(TypeError):
        bus.connect(filters=cast(tuple[CanFilter, ...], (object(),)))
    node = bus.connect()
    with pytest.raises(TypeError):
        node.set_filters(cast(tuple[CanFilter, ...], (object(),)))
    bus.close()
    with pytest.raises(AdapterError):
        bus.connect()


def test_concurrent_senders_deliver_every_frame_once_per_other_node() -> None:
    bus = VirtualCanBus()
    senders = [bus.connect() for _ in range(4)]
    receiver = bus.connect()
    frames = [CanFrame(i, bytes([i % 256])) for i in range(100)]

    def send_indexed(pair: tuple[int, CanFrame]) -> None:
        index, frame = pair
        senders[index % 4].send(frame)

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(send_indexed, enumerate(frames)))

    received = [receiver.recv(timeout=0) for _ in frames]
    assert len([frame for frame in received if frame is not None]) == len(frames)
    assert {frame.arbitration_id for frame in received if frame is not None} == set(
        range(100)
    )


class _SignallingQueue(Queue[CanFrame | object]):
    def __init__(self) -> None:
        super().__init__()
        self.get_started = Event()

    def get(
        self, block: bool = True, timeout: float | None = None
    ) -> CanFrame | object:
        self.get_started.set()
        return super().get(block=block, timeout=timeout)
