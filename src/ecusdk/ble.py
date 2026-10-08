"""Adaptador ELM327 por BLE (extra opcional ``ecusdk[ble]``).

Publica el GATT de los adaptadores ELM327 BLE habituales: servicio ``0BDA``,
escrituras de la app en ``0BDB`` y respuestas por notificación en ``0BDC``.
Las respuestas se notifican en trozos de ``chunk`` bytes, como un adaptador
real sin MTU negociado (20 bytes); la app debe acumularlas hasta ``>``.
"""

from __future__ import annotations

import asyncio
import importlib
import queue
import threading
from collections.abc import Callable
from typing import Any

from ecusdk.elm327 import Elm327Emulator
from ecusdk.errors import AdapterError

SERVICE_UUID = "00000bda-0000-1000-8000-00805f9b34fb"
RX_UUID = "00000bdb-0000-1000-8000-00805f9b34fb"
TX_UUID = "00000bdc-0000-1000-8000-00805f9b34fb"


class _QueueTransport:
    """``ByteTransport`` alimentado por escrituras BLE desde otro hilo."""

    def __init__(self, send: Callable[[bytes], object]) -> None:
        self._incoming: queue.Queue[bytes] = queue.Queue()
        self._send = send

    def feed(self, data: bytes) -> None:
        self._incoming.put(data)

    def read(self, size: int = 4096) -> bytes:
        return self._incoming.get()

    def write(self, data: bytes) -> None:
        self._send(data)

    def close(self) -> None:
        pass


def chunks(data: bytes, size: int) -> list[bytes]:
    """Divide ``data`` en trozos de como máximo ``size`` bytes."""
    if size < 1:
        raise ValueError("chunk debe ser >= 1")
    return [data[i : i + size] for i in range(0, len(data), size)]


async def serve_ble(
    emulator: Elm327Emulator, name: str = "OBDII ecusdk", chunk: int = 20
) -> None:
    """Anuncia el adaptador y atiende comandos hasta que se cancele la tarea."""
    if chunk < 1:
        raise ValueError("chunk debe ser >= 1")
    try:
        bless: Any = importlib.import_module("bless")
    except ImportError as error:
        raise AdapterError(
            "BLE requiere el extra opcional: pip install 'ecusdk[ble]'"
        ) from error
    loop = asyncio.get_running_loop()
    outgoing: asyncio.Queue[bytes] = asyncio.Queue()
    transport = _QueueTransport(
        lambda data: loop.call_soon_threadsafe(outgoing.put_nowait, data)
    )

    def read(characteristic: Any, **_: Any) -> Any:
        return characteristic.value

    def write(characteristic: Any, value: bytes, **_: Any) -> None:
        transport.feed(bytes(value))

    server: Any = bless.BlessServer(name=name, loop=loop)
    server.read_request_func = read
    server.write_request_func = write
    props = bless.GATTCharacteristicProperties
    perms = bless.GATTAttributePermissions
    await server.add_new_service(SERVICE_UUID)
    await server.add_new_characteristic(
        SERVICE_UUID,
        RX_UUID,
        props.write | props.write_without_response,
        None,
        perms.writeable,
    )
    await server.add_new_characteristic(
        SERVICE_UUID, TX_UUID, props.read | props.notify, None, perms.readable
    )
    threading.Thread(
        target=emulator.session().serve, args=(transport,), daemon=True
    ).start()
    await server.start()
    try:
        while True:
            for piece in chunks(await outgoing.get(), chunk):
                server.get_characteristic(TX_UUID).value = bytearray(piece)
                # ponytail: reintento acotado si la cola de notificaciones del
                # SO está llena (~1 s); después se descarta el trozo.
                for _ in range(100):
                    if server.update_value(SERVICE_UUID, TX_UUID):
                        break
                    await asyncio.sleep(0.01)
    finally:
        transport.feed(b"")
        await server.stop()
