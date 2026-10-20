"""Explicit controls, separate from Nautilus and its status-only socket."""

import json
import signal

from .engine import StatusEngine
from .model import ConfigurationError, StorageError
from .offline import OfflineController, usage
from .offline_config import initialize, load_offline
from .onedrive import OneDrive
from .paths import SecureRoot
from .state import ReadStore, StateStore
from .tracking import ChangeTracker
from .view import mount, prove_view


def configure(commands):
    parser = commands.add_parser(
        "offline", help="Explicit individual-file controls for the dedicated test account"
    )
    parser.add_argument("--config", required=True, help="Private offline control JSON")
    actions = parser.add_subparsers(dest="action", required=True)
    for name in ("init", "recover", "jobs", "conflicts", "snapshots", "changes", "resume"):
        actions.add_parser(name)
    for name in ("keep", "retry", "unpin", "free", "status", "queue", "refresh", "queue-tree", "unpin-tree"):
        action = actions.add_parser(name)
        action.add_argument("path", help="Exact file path relative to the verified allowed root")
    view = actions.add_parser("mount", help="Foreground experimental read-only view; Ctrl-C unmounts it")
    view.add_argument(
        "--offline", action="store_true", help="No-network local-only view at the same mountpoint"
    )


def run(args):
    config, settings = load_offline(args.config)
    if args.action in ("jobs", "conflicts", "snapshots", "changes"):
        with ReadStore(config) as store:
            if store.connection.execute("PRAGMA user_version").fetchone()[0] < 3:
                raise ConfigurationError("Run offline recover once to upgrade state before M4 inspection")
            tracker = ChangeTracker(OfflineController(config, store, None, settings))
            print(json.dumps(getattr(tracker, args.action)()))
        return
    if args.action == "status":
        with ReadStore(config) as store:
            controller = OfflineController(config, store, None, settings)
            identity = controller.identity_at(args.path)
            result = StatusEngine(
                config, store, read_path=lambda remote, copy: prove_view(config, settings, remote, copy)
            ).status(identity)
            copy = store.copy(identity)
            print(
                json.dumps(
                    {
                        "state": result.state.value,
                        "reason": result.reason,
                        "pinned": bool(store.row(identity)["pinned"]),
                        "retained_bytes": copy.size if copy else 0,
                    }
                )
            )
        return
    if args.action == "mount":
        stopped = [False]
        original = signal.signal(signal.SIGINT, lambda *unused: stopped.__setitem__(0, True))
        try:
            mount(
                config,
                settings,
                offline=args.offline,
                stop=lambda: stopped[0],
                ready=lambda: print(
                    "Experimental read-only view ready; Ctrl-C unmounts this view only.", flush=True
                ),
            )
        finally:
            signal.signal(signal.SIGINT, original)
        return
    backend = (
        OneDrive(config, settings)
        if args.action in ("init", "keep", "retry", "free", "queue", "refresh", "queue-tree", "resume")
        else None
    )
    if args.action == "init":
        backend.authenticate()
        initialize(config, settings)
    stopped = [False]
    original = signal.signal(signal.SIGINT, lambda *unused: stopped.__setitem__(0, True))
    try:
        with StateStore(config) as store:
            controller = OfflineController(
                config,
                store,
                backend,
                settings,
                progress=lambda **values: print(json.dumps(values), flush=True),
                cancelled=lambda: stopped[0],
            )
            tracker = ChangeTracker(controller)
            if args.action in ("queue", "refresh"):
                getattr(tracker, args.action)(args.path)
            elif args.action == "queue-tree":
                tracker.queue_tree(args.path)
            elif args.action == "unpin-tree":
                tracker.unpin_tree(args.path)
            elif args.action == "resume":
                jobs = tracker.resume()
                print(json.dumps(jobs))
                if any(job["status"] in ("error", "blocked") for job in jobs):
                    raise StorageError("Some jobs need attention; inspect offline jobs and conflicts")
            elif args.action in ("keep", "retry"):
                controller.keep(args.path)
            elif args.action == "unpin":
                controller.unpin(args.path)
            elif args.action == "free":
                controller.free(args.path)
            else:
                controller.recover()
            with SecureRoot(config.storage_root, config.volume_id) as root:
                print(json.dumps({"operation": args.action, "store_allocated_bytes": usage(root)}))
    finally:
        signal.signal(signal.SIGINT, original)
