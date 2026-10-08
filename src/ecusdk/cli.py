"""CLI de ECUSDK."""

from __future__ import annotations

import argparse
import asyncio
import signal
import threading

from ecusdk.ble import serve_ble
from ecusdk.can import CanFrame, SocketCanBus
from ecusdk.config import load_vehicle
from ecusdk.elm327 import Elm327Emulator, TcpServer
from ecusdk.errors import AdapterError, RecordingError
from ecusdk.recording import (
    Record,
    Recorder,
    format_record,
    frame_of,
    read_log,
    replay,
)
from ecusdk.shell import VehicleShell, describe, send_command
from ecusdk.simulation import Vehicle

_CONTROL_PORT = 35001
_REMOTE = ("get", "set", "dtc", "fault", "faults", "request")


def _remote_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--control-port", type=int, default=_CONTROL_PORT)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ecusdk")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="ejecutar un vehículo virtual")
    run.add_argument("config")
    run.add_argument("--request", default=None, help="consulta OBD en hexadecimal")
    run.add_argument("--host", default="127.0.0.1")
    run.add_argument("--port", type=int, default=35000, help="puerto ELM327 TCP")
    run.add_argument(
        "--control-port",
        type=int,
        default=_CONTROL_PORT,
        help="puerto del shell de control; 0 lo desactiva",
    )
    run.add_argument("--once", action="store_true", help="no iniciar el servidor")
    run.add_argument("--record", default=None, help="grabar la sesión en un .eculog")
    run.add_argument(
        "--ble", action="store_true", help="anunciar además un ELM327 BLE (ecusdk[ble])"
    )
    run.add_argument("--ble-name", default="OBDII ecusdk", help="nombre BLE anunciado")
    run.add_argument(
        "--ble-chunk", type=int, default=20, help="bytes por notificación BLE"
    )
    replay_parser = commands.add_parser("replay", help="reproducir un .eculog")
    replay_parser.add_argument("log")
    replay_parser.add_argument(
        "--speed", type=float, default=1.0, help="multiplicador de velocidad"
    )
    replay_parser.add_argument(
        "--fast", action="store_true", help="sin esperas entre registros"
    )
    replay_parser.add_argument(
        "--type", action="append", dest="types", help="filtrar por tipo de registro"
    )
    replay_parser.add_argument(
        "--socketcan",
        default=None,
        metavar="IFACE",
        help="reinyectar los frames en esta interfaz SocketCAN (explícita)",
    )
    replay_parser.add_argument("--bus", default=None, help="sólo frames de este bus")
    inspect = commands.add_parser(
        "inspect", help="describir un TOML o el vehículo en ejecución"
    )
    inspect.add_argument("config", nargs="?")
    _remote_options(inspect)
    shell = commands.add_parser("shell", help="shell interactivo del vehículo")
    _remote_options(shell)
    for name in _REMOTE:
        remote = commands.add_parser(name, help=f"comando '{name}' del shell remoto")
        remote.add_argument("words", nargs="*" if name == "faults" else "+")
        _remote_options(remote)
    args = parser.parse_args(argv)

    if args.command == "run":
        return _run(args)
    if args.command == "replay":
        return _replay(args)
    if args.command == "inspect" and args.config is not None:
        print(describe(load_vehicle(args.config)))
        return 0
    if args.command == "shell":
        return _shell(args.host, args.control_port)
    line = (
        "inspect"
        if args.command == "inspect"
        else " ".join([args.command, *args.words])
    )
    try:
        response = send_command(args.host, args.control_port, line)
    except OSError as error:
        parser.exit(1, f"ecusdk: sin conexión al shell de control: {error}\n")
    print(response)
    return 1 if response.startswith("ERROR") else 0


def _replay(args: argparse.Namespace) -> int:
    try:
        log = read_log(args.log)
    except (OSError, RecordingError) as error:
        print(f"ecusdk: {error}")
        return 1
    bus = SocketCanBus(args.socketcan) if args.socketcan else None

    def handle(record: Record) -> None:
        if args.bus is not None and record.get("bus", args.bus) != args.bus:
            return
        if bus is not None and record["type"] == "frame":
            bus.send(frame_of(record))
        if args.types is None or record["type"] in args.types:
            print(format_record(record), flush=True)

    try:
        replay(log, handle, speed=0 if args.fast else args.speed)
    except KeyboardInterrupt:
        pass
    finally:
        if bus is not None:
            bus.close()
    return 0


def _run(args: argparse.Namespace) -> int:
    vehicle = load_vehicle(args.config)
    recorder = Recorder(vehicle, args.record) if args.record else None
    try:
        return _serve(args, vehicle)
    finally:
        if recorder is not None:
            recorder.close()


def _serve(args: argparse.Namespace, vehicle: Vehicle) -> int:
    vehicle.start()
    print(f"{vehicle.name}: running")
    if args.request is not None:
        request = bytes.fromhex(args.request)
        frame = CanFrame(vehicle.ecus[0].request_id, bytes((len(request),)) + request)
        response = vehicle.exchange(next(iter(vehicle.buses)), frame)
        if response is None:
            print("NO DATA")
        else:
            print(response.data.hex(" ").upper())
        return 0
    if args.once:
        return 0
    servers = [TcpServer(Elm327Emulator(vehicle), args.host, args.port)]
    if args.control_port:
        servers.append(TcpServer(VehicleShell(vehicle), args.host, args.control_port))
        threading.Thread(target=servers[1].serve_forever, daemon=True).start()
        print(f"control: {args.host}:{args.control_port}")
    print(f"ELM327: {args.host}:{args.port}")
    if args.ble:
        threading.Thread(target=servers[0].serve_forever, daemon=True).start()
        print(f"BLE: {args.ble_name}")
    # SIGTERM (docker stop, systemd) cierra igual que Ctrl+C: servidores,
    # vehículo y grabación terminan de forma ordenada.
    previous = signal.signal(signal.SIGTERM, _interrupt)
    try:
        if args.ble:
            asyncio.run(
                serve_ble(Elm327Emulator(vehicle), args.ble_name, args.ble_chunk)
            )
        else:
            servers[0].serve_forever()
    except KeyboardInterrupt:
        pass
    except AdapterError as error:
        print(f"ecusdk: {error}")
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous)
        for server in servers:
            server.close()
        vehicle.stop()
    return 0


def _interrupt(signum: int, frame: object) -> None:
    raise KeyboardInterrupt


def _shell(host: str, port: int) -> int:
    while True:
        try:
            line = input("ECUSDK > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if line in ("exit", "quit"):
            return 0
        if line:
            try:
                print(send_command(host, port, line))
            except OSError as error:
                print(f"ERROR: sin conexión al shell de control: {error}")
                return 1


if __name__ == "__main__":
    raise SystemExit(main())
