"""CLI de ECUSDK."""

from __future__ import annotations

import argparse

from ecusdk.can import CanFrame
from ecusdk.config import load_vehicle
from ecusdk.elm327 import Elm327Emulator, TcpElm327Server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ecusdk")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="ejecutar un vehículo virtual")
    run.add_argument("config")
    run.add_argument("--request", default=None, help="consulta OBD en hexadecimal")
    run.add_argument("--host", default="127.0.0.1")
    run.add_argument("--port", type=int, default=35000)
    run.add_argument("--once", action="store_true", help="no iniciar el servidor")
    args = parser.parse_args(argv)

    if args.command == "run":
        vehicle = load_vehicle(args.config)
        vehicle.start()
        print(f"{vehicle.name}: running")
        if args.request is not None:
            request = bytes.fromhex(args.request)
            frame = CanFrame(
                vehicle.ecus[0].request_id, bytes((len(request),)) + request
            )
            response = vehicle.exchange(next(iter(vehicle.buses)), frame)
            if response is None:
                print("NO DATA")
            else:
                print(response.data.hex(" ").upper())
            return 0
        if args.once:
            return 0
        server = TcpElm327Server(Elm327Emulator(vehicle), args.host, args.port)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.close()
            vehicle.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
