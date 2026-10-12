"""CLI entry for vhctl."""

from __future__ import annotations

import argparse
import logging
import sys


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="[%(levelname)s] %(message)s",
        stream=sys.stderr,
    )


def main(argv: list[str] | None = None) -> int:
    _setup_logging()
    parser = argparse.ArgumentParser(prog="vhctl", description="VirtualHere Client API controller")
    sub = parser.add_subparsers(dest="command", required=True)

    wait_p = sub.add_parser("wait", help="Wait until the client IPC is ready")
    wait_p.add_argument("--timeout", type=float, default=60.0)

    sub.add_parser("configure", help="Apply autofind/autouse/manualhub via Client API")
    sub.add_parser("discover", help="Discover devices and update addon options")
    sub.add_parser("sync-devices", help="Sync per-device USE/AUTO USE via DEVICE INFO gate")
    sub.add_parser("apply", help="configure + sync-devices + discover once")

    watch_p = sub.add_parser(
        "watch",
        help="Watch options and apply USB/config changes without restarting the app",
    )
    watch_p.add_argument("--poll", type=float, default=2.0, help="Options poll interval seconds")
    watch_p.add_argument(
        "--discover-interval",
        type=float,
        default=30.0,
        help="Periodic USB list refresh interval seconds",
    )
    watch_p.add_argument(
        "--health-port",
        type=int,
        default=8199,
        help="HTTP port for Supervisor watchdog /health",
    )

    args = parser.parse_args(argv)

    # Import after logging setup so module logs use the configured handlers.
    from . import sync, watch as watch_mod

    if args.command == "wait":
        return sync.wait(timeout=args.timeout)
    if args.command == "configure":
        return sync.configure()
    if args.command == "discover":
        return sync.discover()
    if args.command == "sync-devices":
        return sync.sync_devices()
    if args.command == "apply":
        return sync.apply_all()
    if args.command == "watch":
        return watch_mod.watch(
            poll_seconds=args.poll,
            discover_seconds=args.discover_interval,
            health_port=args.health_port,
        )
    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
