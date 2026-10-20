"""Explicit controls, separate from Nautilus and its status-only socket."""

import json
import signal

from .model import ConfigurationError, StorageError
from .offline import OfflineController, usage
from .offline_config import initialize, load_offline
from .onedrive import OneDrive
from .paths import SecureRoot
from .state import ReadStore, StateStore
from .tracking import ChangeTracker
from .view import mount


def configure(commands):
    parser = commands.add_parser(
        "offline", help="Explicit individual-file controls for the dedicated test account"
    )
    parser.add_argument("--config", required=True, help="Private offline control JSON")
    actions = parser.add_subparsers(dest="action", required=True)
    for name in (
        "init",
        "recover",
        "jobs",
        "conflicts",
        "snapshots",
        "changes",
        "resume",
        "archives",
        "doctor",
    ):
        actions.add_parser(name)
    for name in (
        "keep",
        "retry",
        "unpin",
        "free",
        "status",
        "queue",
        "refresh",
        "queue-tree",
        "unpin-tree",
        "verify",
    ):
        action = actions.add_parser(name)
        action.add_argument("path", help="Exact file path relative to the verified allowed root")
    resolve = actions.add_parser("resolve", help="Archive every retained version and retire a conflict")
    resolve.add_argument("path")
    resolve.add_argument(
        "--archive",
        action="store_true",
        required=True,
        help="Acknowledge retaining all versions in a private archive",
    )
    resolve.add_argument(
        "--retry-remote", action="store_true", help="Queue a fresh download of the same live cloud identity"
    )
    export = actions.add_parser("export-archive", help="Copy an archive into a new private directory")
    export.add_argument("archive_id")
    export.add_argument("destination")
    view = actions.add_parser("mount", help="Foreground experimental read-only view; Ctrl-C unmounts it")
    view.add_argument(
        "--offline", action="store_true", help="No-network local-only view at the same mountpoint"
    )


def run(args):
    config, settings = load_offline(args.config)
    if args.action in ("archives", "export-archive", "doctor"):
        from .diagnostics import doctor
        from .recovery import ConflictRecovery

        with ReadStore(config) as store:
            controller = OfflineController(config, store, None, settings)
            if args.action == "doctor":
                print(json.dumps(doctor(controller)))
            elif store.connection.execute("PRAGMA user_version").fetchone()[0] < 4:
                raise ConfigurationError("Run offline recover to upgrade state before archive inspection")
            elif args.action == "archives":
                print(json.dumps(ConflictRecovery(controller).archives()))
            else:
                stopped = [False]
                original = signal.signal(signal.SIGINT, lambda *unused: stopped.__setitem__(0, True))
                controller.cancelled = lambda: stopped[0]
                try:
                    print(json.dumps(ConflictRecovery(controller).export(args.archive_id, args.destination)))
                finally:
                    signal.signal(signal.SIGINT, original)
        return
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
            from .verification import status_engine

            result = status_engine(config, store, settings).status(identity)
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
        or args.action == "resolve"
        and args.retry_remote
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
            if args.action == "resolve":
                from .recovery import ConflictRecovery

                print(
                    json.dumps(
                        ConflictRecovery(controller).resolve(args.path, retry_remote=args.retry_remote)
                    )
                )
            elif args.action == "verify":
                from .verification import verify

                print(json.dumps(verify(controller, args.path)))
            elif args.action in ("queue", "refresh"):
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
