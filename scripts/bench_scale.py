"""Reproduce bounded folder/desktop work and idle-peer socket latency, without an account.

    python scripts/bench_scale.py --entries 1000 10000 50000 --rounds 5 --json

Entry counts include the root. The fake backend enforces the production tree
limit; SQLite and secure local directory checks are real. No FUSE, credentials,
network, user services or desktop are accessed. Times describe this machine.
"""

import argparse
import json
import os
import socket
import sys
import tempfile
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cloudmarks import offline_config  # noqa: E402
from cloudmarks.activity import Activity  # noqa: E402
from cloudmarks.config import Config  # noqa: E402
from cloudmarks.desktop import MAX_TRACKED, Background  # noqa: E402
from cloudmarks.engine import StatusEngine  # noqa: E402
from cloudmarks.folders import FolderObserver  # noqa: E402
from cloudmarks.helper import serve  # noqa: E402
from cloudmarks.ipc import request  # noqa: E402
from cloudmarks.limits import TREE_ENTRIES  # noqa: E402
from cloudmarks.model import FileIdentity, RemoteFile, ScopeError, StorageError  # noqa: E402
from cloudmarks.onedrive import Observation  # noqa: E402
from cloudmarks.paths import MARKER  # noqa: E402
from cloudmarks.state import StateStore  # noqa: E402
from cloudmarks.tracking import manifest  # noqa: E402
from scripts.bench_status import percentiles  # noqa: E402


def folder(base, count, rounds):
    disk, state = base / "disk", base / "state"
    disk.mkdir(mode=0o700)
    state.mkdir(mode=0o700)
    (disk / MARKER).write_text("bench-volume\n")
    (disk / "local").mkdir()
    config = Config(
        "bench-drive",
        "bench-root",
        (),
        disk,
        "bench-volume",
        state / "state.sqlite3",
        case_sensitive=True,
        max_age_seconds=3600,
    )
    root = RemoteFile(
        FileIdentity("bench-drive", "folder"), "bench-root", "folder", "r1", 0, time.time(), is_directory=True
    )
    observations = [Observation(root, "sha256Hash", b"")]
    observations.extend(
        Observation(
            replace(
                root,
                identity=FileIdentity("bench-drive", str(i)),
                relative_path=f"folder/{i:06}",
                is_directory=False,
            ),
            "sha256Hash",
            b"",
        )
        for i in range(1, count)
    )

    def tree(*args, limit, **kwargs):
        if len(observations) > limit:
            raise ScopeError("Artificial tree exceeds production enumeration limit")
        return observations

    observer = FolderObserver(config, {}, Activity, backend=SimpleNamespace(tree=tree))
    with StateStore(config) as store:
        store.observe(root)
        # Artificial recorded intent exercises the engine's aggregate guard.
        intent = {"manifest": json.dumps(manifest(observations)), "scanned_at": time.time(), "pinned": True}
        store.folder_intent = lambda identity: intent
        engine = StatusEngine(config, store)
        samples, engine_samples = [], []
        with patch("cloudmarks.folders.attest_view", return_value=True):
            for _ in range(rounds):
                start = time.perf_counter()
                try:
                    result, _ = observer.check("folder", "offline")
                except ScopeError:
                    result = {"state": "unknown"}
                samples.append((time.perf_counter() - start) * 1000)
                start = time.perf_counter()
                engine_result = engine.folder_status(root)
                engine_samples.append((time.perf_counter() - start) * 1000)
    return {
        "entries": count,
        "observer": dict(percentiles(samples), state=result["state"]),
        "engine": dict(percentiles(engine_samples), state=engine_result.state.value),
        "desktop": desktop_queue(count),
        "tree_limit": TREE_ENTRIES,
    }


def desktop_queue(count):
    # Freeze the consumer to model a burst of Files callbacks: measure actual
    # admission/backpressure, not just arithmetic against MAX_TRACKED.
    with patch("cloudmarks.desktop.threading.Thread"):
        worker = Background("/artificial/settings")
    start = time.perf_counter()
    accepted = sum(worker.submit(f"file:///artificial/folder/{i}") for i in range(count))
    elapsed = (time.perf_counter() - start) * 1000
    worker.close()
    return {
        "accepted": accepted,
        "deferred": count - accepted,
        "burst_ms": round(elapsed, 2),
        "capacity": MAX_TRACKED,
    }


def sockets(base, rounds, idle):
    stop, ready = threading.Event(), threading.Event()
    settings = {"socket": str(base / "status.sock"), "mode": "fixture"}

    class Service:
        def handle(self, value):
            return {
                "version": 1,
                "results": {
                    p: {"state": "cloud_only", "reason": "Artificial fixture", "valid_for_ms": 1000}
                    for p in value["paths"]
                },
            }

    errors = []

    def run():
        try:
            serve(settings, None, stop=stop.is_set, ready=ready.set, service=Service())
        except Exception as error:
            errors.append(repr(error))
            ready.set()

    thread = threading.Thread(target=run)
    thread.start()
    samples = []
    try:
        if not ready.wait(5) or errors:
            raise RuntimeError(f"Artificial helper failed to start: {errors}")
        for _ in range(rounds):
            with socket.socket(socket.AF_UNIX) as peer:
                if idle:
                    peer.connect(settings["socket"])
                    peer.sendall(b"{")
                    time.sleep(0.02)
                start = time.perf_counter()
                response = request(settings["socket"], ["file"])
                if response["file"]["state"] != "cloud_only":
                    raise RuntimeError("Artificial helper request failed or exceeded the client deadline")
                samples.append((time.perf_counter() - start) * 1000)
    finally:
        stop.set()
        thread.join(3)
    if errors or thread.is_alive():
        raise RuntimeError(f"Artificial helper failed to stop: {errors}")
    return percentiles(samples)


def volume(base, rounds):
    """Compare a cold proof with a controlled 100 ms findmnt, no actual mount."""
    config = SimpleNamespace(storage_root=base)
    info = base.stat()
    row = {
        "uuid": "artificial",
        "target": "/",
        "options": "rw",
        "maj:min": f"{os.major(info.st_dev)}:{os.minor(info.st_dev)}",
    }
    settings = {"storage_mount_uuid": "artificial", "storage_mount_target": "/"}

    def findmnt(*args, **kwargs):
        time.sleep(0.1)
        return SimpleNamespace(stdout=json.dumps({"filesystems": [row]}))

    sync, background = [], []
    with (
        patch.object(offline_config, "check_binding"),
        patch.object(offline_config.subprocess, "run", side_effect=findmnt),
    ):
        for _ in range(rounds):
            start = time.perf_counter()
            offline_config.check_volume(config, settings)
            sync.append((time.perf_counter() - start) * 1000)
            offline_config._VOLUME_PROOFS.clear()
            start = time.perf_counter()
            try:
                offline_config.check_volume(config, settings, reuse_seconds=10)
            except StorageError:
                pass
            background.append((time.perf_counter() - start) * 1000)
            worker = getattr(offline_config, "_VOLUME_THREAD", None)
            if worker:
                worker.join(2)
                if worker.is_alive():
                    raise RuntimeError("Artificial volume worker failed to finish")
    return {
        "synthetic_findmnt_ms": 100,
        "synchronous_fresh_proof": percentiles(sync),
        "background_cold_request": percentiles(background),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entries", nargs="+", type=int, default=[1000, 10000, 50000])
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.rounds < 1 or any(n < 1 or n > 50000 for n in args.entries):
        parser.error("--rounds must be positive; --entries must be between 1 and 50000")
    with tempfile.TemporaryDirectory(prefix="cm-scale-", dir="/tmp") as temporary:
        base = Path(temporary)
        folders = []
        for n in args.entries:
            work = base / str(n)
            work.mkdir()
            folders.append(folder(work, n, args.rounds))
        result = {
            "folders": folders,
            "socket": sockets(base, args.rounds, False),
            "socket_with_idle_peer": sockets(base, args.rounds, True),
            "volume": volume(base, args.rounds),
        }
    print(json.dumps(result, indent=None if args.json else 2))


if __name__ == "__main__":
    main()
