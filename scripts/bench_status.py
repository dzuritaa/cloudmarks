"""Measure status-socket latency with a cold page cache, inline versus background hashing.

Builds a disposable store in a new temporary directory with artificial files,
then times OfflineStatusService.handle() for 16-path batches, the batch size
Nautilus sends. "Cold" evicts each retained file from the page cache with
posix_fadvise(DONTNEED) and starts with an empty hash memo. Mount attestation
is stubbed: this measures hashing and state access, not FUSE or findmnt.

No account, network, mount or desktop session is used.

    python scripts/bench_status.py --files 64 --size-mib 4
    python scripts/bench_status.py --dir /path/on/the/offline/disk --json
"""

import argparse
import hashlib
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cloudmarks.config import Config  # noqa: E402
from cloudmarks.helper import OfflineStatusService  # noqa: E402
from cloudmarks.model import FileIdentity, RemoteFile  # noqa: E402
from cloudmarks.offline import OfflineController  # noqa: E402
from cloudmarks.onedrive import Observation  # noqa: E402
from cloudmarks.paths import MARKER  # noqa: E402
from cloudmarks.state import ReadStore, StateStore  # noqa: E402

BATCH = 16


class Backend:
    """Artificial cloud: deterministic content per path, SHA-256 server checksum."""

    def __init__(self, config, size):
        self.config, self.size = config, size

    def content(self, path):
        seed = hashlib.sha256(path.encode()).digest()
        return (seed * (self.size // len(seed) + 1))[: self.size]

    def describe(self, path, **unused):
        data = self.content(path)
        remote = RemoteFile(
            FileIdentity(self.config.expected_drive_id, "item-" + hashlib.sha1(path.encode()).hexdigest()),
            self.config.allowed_root_id,
            path,
            "v1",
            len(data),
            time.time(),
        )
        return Observation(remote, "sha256Hash", hashlib.sha256(data).digest())

    def chunks(self, observation, *, cancelled=lambda: False):
        data = self.content(observation.remote.relative_path)
        for offset in range(0, len(data), 1024 * 1024):
            yield data[offset : offset + 1024 * 1024]


def evict(root):
    for path in root.rglob("*"):
        if path.is_file():
            fd = os.open(path, os.O_RDONLY)
            try:
                os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
            finally:
                os.close(fd)


def percentiles(values):
    ordered = sorted(values)

    def pick(fraction):
        return ordered[min(len(ordered) - 1, int(fraction * len(ordered)))]

    return {
        "p50_ms": round(statistics.median(ordered), 2),
        "p95_ms": round(pick(0.95), 2),
        "p99_ms": round(pick(0.99), 2),
        "max_ms": round(ordered[-1], 2),
    }


def build(base, files, size):
    disk, private = base / "disk", base / "state"
    disk.mkdir(mode=0o700)
    private.mkdir(mode=0o700)
    (disk / MARKER).write_text("bench-volume\n")
    for name in ("objects", "local", "recovery"):
        (disk / name).mkdir()
    config = Config(
        "bench-drive",
        "bench-root",
        ("blocked",),
        disk,
        "bench-volume",
        private / "state.sqlite3",
        case_sensitive=True,
        max_age_seconds=3600,
    )
    settings = {
        "budget_bytes": files * size * 3 + 64 * 1024 * 1024,
        "min_free_bytes": 0,
        "visible_root": str(base / "view"),
    }
    paths = [f"bench/file-{index:04}.bin" for index in range(files)]
    with StateStore(config) as store:
        controller = OfflineController(
            config,
            store,
            Backend(config, size),
            settings,
            volume_check=lambda *a: None,
            unmounted=lambda *a: None,
        )
        for path in paths:
            controller.keep(path)
    return config, settings, paths


def measure(config, settings, paths, *, background, rounds):
    batches = [paths[i : i + BATCH] for i in range(0, len(paths), BATCH)]
    latencies, checking, settle = [], 0, []
    with ReadStore(config) as reader, patch("cloudmarks.view.attest_view", return_value=True):
        for _ in range(rounds):
            evict(config.storage_root)
            service = OfflineStatusService({"visible_root": settings["visible_root"]}, reader, settings)
            if not background:
                from cloudmarks.verification import status_engine

                service.engine = status_engine(config, reader, settings)
            try:
                started = time.monotonic()
                final = set()
                # Re-ask like Nautilus does until every path has a final answer.
                while len(final) < len(paths) and time.monotonic() - started < 120:
                    for batch in batches:
                        begin = time.perf_counter()
                        response = service.handle({"version": 1, "op": "status", "paths": batch})
                        latencies.append((time.perf_counter() - begin) * 1000)
                        for path, result in response["results"].items():
                            if result["state"] == "kept_offline":
                                final.add(path)
                            else:
                                checking += 1
                    if len(final) < len(paths):
                        time.sleep(0.05)
                settle.append((time.monotonic() - started) * 1000)
            finally:
                service.close()
    return dict(
        percentiles(latencies),
        requests=len(latencies),
        non_final_answers=checking,
        all_final_ms=round(statistics.median(settle), 1),
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--files", type=int, default=64)
    parser.add_argument("--size-mib", type=float, default=4.0)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--dir", type=Path, help="Parent for the disposable store (default: system temp)")
    parser.add_argument("--json", action="store_true", help="Print one JSON object")
    args = parser.parse_args(argv)
    size = int(args.size_mib * 1024 * 1024)
    if not 0 < size <= 8 * 1024 * 1024:
        parser.error("--size-mib must be above 0 and at most 8 (larger files use explicit proofs)")
    with tempfile.TemporaryDirectory(prefix="cm-bench-", dir=args.dir) as temp:
        config, settings, paths = build(Path(temp), args.files, size)
        result = {
            "files": args.files,
            "size_mib": args.size_mib,
            "batch": BATCH,
            "inline": measure(config, settings, paths, background=False, rounds=args.rounds),
            "background": measure(config, settings, paths, background=True, rounds=args.rounds),
        }
    if args.json:
        print(json.dumps(result))
        return
    print(
        f"{args.files} files x {args.size_mib} MiB, batches of {BATCH}, cold page cache, {args.rounds} rounds"
    )
    for mode in ("inline", "background"):
        value = result[mode]
        print(
            f"  {mode:<10} per-request p50 {value['p50_ms']:>8} ms  p95 {value['p95_ms']:>8} ms  "
            f"p99 {value['p99_ms']:>8} ms  max {value['max_ms']:>8} ms  "
            f"all final after {value['all_final_ms']} ms"
        )


if __name__ == "__main__":
    main()
