"""Local fixture and desktop commands. The demo cannot discover or use cloud credentials."""

import argparse
import json
import tempfile
import time
from pathlib import Path

from . import __version__
from .config import Config, load_config
from .engine import StatusEngine
from .fixture import FixtureBackend, FixtureController
from .model import CloudMarksError, FileIdentity
from .paths import MARKER
from .state import StateStore


def demo():
    with tempfile.TemporaryDirectory(prefix="cloudmarks-fixtures-") as directory:
        root = Path(directory)
        storage = root / "disk"
        state = root / "state"
        storage.mkdir(mode=0o700)
        state.mkdir(mode=0o700)
        (storage / MARKER).write_text("fixture-volume\n")
        config = Config(
            "fixture-test-drive",
            "fixture-root",
            ("fixture-personal-drive",),
            storage,
            "fixture-volume",
            state / "state.sqlite3",
        )
        now = [time.time()]
        backend = FixtureBackend(config, clock=lambda: now[0])
        identity = FileIdentity(config.expected_drive_id, "fixture-item")
        remote = backend.put(identity, "disposable document.txt", b"CloudMarks fixture content.\n")
        with StateStore(config) as store:
            store.observe(remote)
            engine = StatusEngine(config, store, read_path=backend.prove_read_path, clock=lambda: now[0])
            controller = FixtureController(config, store, backend)
            events = []

            def record(label):
                status = engine.status(identity)
                events.append({"step": label, "state": status.state.value, "reason": status.reason})

            record("Before download")
            backend.chunk_hook = lambda index: record("During download") if index == 0 else None
            controller.download(identity)
            record("Complete download")
            store.set_pin(identity, True)
            record("Persistent pin intent")
            now[0] += config.max_age_seconds + 1
            record("Remote evidence expired")
            print(
                json.dumps(
                    {"mode": "fixture-only; no desktop or cloud operations", "events": events}, indent=2
                )
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=f"CloudMarks {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("demo", help="Run an entirely local, temporary fixture demonstration")
    check = commands.add_parser("check-config", help="Validate TOML syntax and account/root settings only")
    check.add_argument("path", type=Path)
    service = commands.add_parser(
        "serve", help="Run the read-only fixture or test-account status helper (foreground)"
    )
    service.add_argument("--settings", type=Path, required=True)
    fixture = commands.add_parser("fixture-session", help="Create disposable fixtures and serve their status")
    fixture.add_argument(
        "--directory", type=Path, required=True, help="New absolute private directory (short path)"
    )
    fixture.add_argument("--count", type=int, default=0, help="Additional large-folder fixtures, 0..10000")
    fixture.add_argument(
        "--cycle-seconds", type=float, default=0, help="Artificial pin transition after this delay"
    )
    deploy = commands.add_parser(
        "install", help="Install for this user, initially disabled; no service/restart"
    )
    deploy.add_argument("--settings", type=Path, required=True)
    commands.add_parser("enable", help="Enable read-only badges; never changes the mount")
    commands.add_parser("disable", help="Clear badges without changing the mount")
    commands.add_parser("uninstall", help="Remove owned deployment files, preserving data")
    from .offline_cli import configure

    configure(commands)
    args = parser.parse_args()
    try:
        if args.command == "offline":
            from .offline_cli import run

            run(args)
        elif args.command == "demo":
            demo()
        elif args.command in ("install", "enable", "disable", "uninstall"):
            from . import install

            if args.command == "install":
                path = install.install(args.settings)
                print(
                    f"Installed disabled. Settings: {path}. No Files restart or service enablement occurred."
                )
            elif args.command == "uninstall":
                install.uninstall()
                print("Owned deployment removed; fixture data and mounts were preserved.")
            else:
                install.set_enabled(args.command == "enable")
                print(f"CloudMarks badges {args.command}d; mounts were not changed.")
        elif args.command == "serve":
            from .helper import run
            from .ipc import load_settings

            run(load_settings(args.settings))
        elif args.command == "fixture-session":
            from .fixture_session import create
            from .helper import serve
            from .ipc import validate_settings

            if (
                not args.directory.is_absolute()
                or ".." in args.directory.parts
                or not 0 <= args.count <= 10000
                or not 0 <= args.cycle_seconds <= 3600
            ):
                parser.error("Expected a new absolute directory, count 0..10000 and cycle delay 0..3600")
            settings, store = create(args.directory, count=args.count)
            started = time.monotonic()
            transitioned = False

            def tick():
                nonlocal transitioned
                if (
                    args.cycle_seconds
                    and not transitioned
                    and time.monotonic() - started >= args.cycle_seconds
                ):
                    store.set_pin(FileIdentity(store.config.expected_drive_id, "07-refresh.txt"), True)
                    transitioned = True

            print(
                f"Artificial fixture session ready. Settings: {args.directory / 'desktop.json'}", flush=True
            )
            try:
                serve(validate_settings(settings), store, tick=tick)
            finally:
                store.close()
        else:
            load_config(args.path)
            print("Configuration version 1 is valid; storage and account connectivity were not checked.")
    except KeyboardInterrupt:
        if args.command == "offline":
            parser.exit(
                130,
                "Offline command interrupted; retained data and any incomplete export remain available.\n",
            )
        parser.exit(0, "Fixture helper stopped. No mount or desktop process was changed.\n")
    except (CloudMarksError, OSError) as error:
        parser.exit(1, f"CloudMarks: {error}\n")


if __name__ == "__main__":
    main()
