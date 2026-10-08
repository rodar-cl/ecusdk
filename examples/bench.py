"""Micro-benchmarks del README. Ejecuta: uv run python examples/bench.py"""

import socket
import statistics
import subprocess
import threading
import time

from ecusdk import (
    CanFrame,
    Elm327Emulator,
    IsoTpTransport,
    TcpElm327Server,
    VirtualClock,
)
from ecusdk.config import load_vehicle


def report(name, samples):
    samples = sorted(samples)
    p50 = statistics.median(samples) * 1e6
    p99 = samples[int(len(samples) * 0.99)] * 1e6
    print(f"{name:<32} p50 {p50:8.1f} µs   p99 {p99:8.1f} µs")


def timed(fn, n):
    samples = []
    for _ in range(n):
        start = time.perf_counter()
        fn()
        samples.append(time.perf_counter() - start)
    return samples


vehicle = load_vehicle("examples/demo-car.toml")
vehicle.start()
bus = next(iter(vehicle.buses))
rpm = CanFrame(0x7E0, bytes.fromhex("02 01 0C"))
vin = CanFrame(0x7E0, bytes.fromhex("02 09 02"))
report("OBD 01 0C (single frame)", timed(lambda: vehicle.exchange(bus, rpm), 20000))
report(
    "OBD 09 02 VIN (multi-frame)", timed(lambda: vehicle.exchange_isotp(bus, vin), 5000)
)

clock = VirtualClock()
tx = IsoTpTransport(0x700, 0x701, clock=clock)
rx = IsoTpTransport(0x701, 0x700, clock=clock)
payload = bytes(4095)


def transfer():
    tx.send(payload)
    done = None
    while done is None:
        for frame in tx.poll():
            done = rx.receive(frame) or done
        for frame in rx.poll():
            tx.receive(frame)


report("ISO-TP 4095 B (586 frames)", timed(transfer, 200))

server = TcpElm327Server(Elm327Emulator(vehicle), "127.0.0.1", 0)
threading.Thread(target=server.serve_forever, daemon=True).start()
time.sleep(0.3)
client = socket.create_connection(("127.0.0.1", server.port))
client.sendall(b"ATE0\r")
client.recv(64)


def elm_query():
    client.sendall(b"010C\r")
    buffer = b""
    while not buffer.endswith(b">"):
        buffer += client.recv(64)


report("ELM327 TCP 010C (round-trip)", timed(elm_query, 5000))
server.close()

cli = ["ecusdk", "run", "examples/demo-car.toml", "--request", "01 0C"]
report(
    "CLI ecusdk run --request",
    timed(lambda: subprocess.run(cli, check=True, capture_output=True), 10),
)
