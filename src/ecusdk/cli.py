"""CLI de ECUSDK."""

from __future__ import annotations

import argparse
import threading

from ecusdk.can import CanFrame
from ecusdk.config import load_vehicle
from ecusdk.elm327 import Elm327Emulator, TcpServer
from ecusdk.shell import VehicleShell, describe, send_command

_CONTROL_PORT = 35001
_REMOTE = ("get", "set", "dtc", "fault", "faults")


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


def _run(args: argparse.Namespace) -> int:
    vehicle = load_vehicle(args.config)
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
    try:
        servers[0].serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for server in servers:
            server.close()
        vehicle.stop()
    return 0


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
