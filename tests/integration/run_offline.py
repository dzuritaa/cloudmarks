#!/usr/bin/env python3
"""Opt-in M3 test against one existing disposable file in the dedicated account.

Explicit private config only. No cloud fixture creation, upload or deletion.
Uses its own newly initialized store/view; never the daily OneDrive mount.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from cloudmarks.engine import StatusEngine
from cloudmarks.model import StorageError
from cloudmarks.offline import OfflineController
from cloudmarks.offline_config import check_volume, initialize, load_offline
from cloudmarks.onedrive import OneDrive
from cloudmarks.state import ReadStore, StateStore
from cloudmarks.view import mount, prove_view


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--path", required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument(
        "--reuse-owned-store",
        action="store_true",
        help="Revalidate this exact bound test store before rerunning",
    )
    args = parser.parse_args()
    config, settings = load_offline(args.config)
    backend = OneDrive(config, settings)
    before = backend.describe(args.path)
    if args.reuse_owned_store:
        check_volume(config, settings)
    else:
        initialize(config, settings)  # New store only; never adopt existing files.
    progress = []
    with StateStore(config) as store:
        controller = OfflineController(
            config, store, backend, settings, progress=lambda **values: progress.append(values)
        )
        copy = controller.keep(args.path)
    expected = copy.sha256
    results = {
        "scope": "existing disposable file; no cloud writes or deletes",
        "bytes": copy.size,
        "server_checksum": before.kind,
        "download_sha256": expected,
        "progress_events": len(progress),
        "mounts": [],
    }

    def mounted(offline):
        ready, stopped = threading.Event(), threading.Event()
        errors = []

        def worker():
            try:
                mount(config, settings, offline=offline, ready=ready.set, stop=stopped.is_set)
            except Exception as error:
                errors.append(error)
                ready.set()

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        if not ready.wait(25) or errors:
            raise StorageError("Experimental view failed; inspect private integration diagnostics")
        path = Path(settings["visible_root"]) / args.path
        try:
            # A child deadline prevents a bad FUSE read from hanging this test.
            code = "import pathlib,hashlib,sys;print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())"
            output = subprocess.check_output(
                [sys.executable, "-c", code, str(path)], timeout=20, text=True
            ).strip()
            assert output == expected
            readonly = {}
            for name, operation in {
                "modify": lambda: path.open("r+b"),
                "create": lambda: path.with_name("must-not-create").open("xb"),
                "rename": lambda: path.rename(path.with_name("must-not-rename")),
                "delete": path.unlink,
            }.items():
                try:
                    value = operation()
                    if hasattr(value, "close"):
                        value.close()
                except OSError as error:
                    readonly[name] = error.errno == 30
                else:
                    readonly[name] = False
            assert all(readonly.values()), readonly
            with ReadStore(config) as reader:
                status = StatusEngine(
                    config, reader, read_path=lambda remote, copy: prove_view(config, settings, remote, copy)
                ).status(copy.identity)
                assert status.state.value == "kept_offline", status.reason
            with StateStore(config) as store:
                controller = OfflineController(config, store, backend, settings)
                try:
                    controller.free(args.path)
                except StorageError:
                    blocked = True
                else:
                    blocked = False
                assert blocked
            return {
                "offline_local_only": offline,
                "sha256": output,
                "read_only": readonly,
                "badge": status.state.value,
                "controls_refused_while_mounted": blocked,
            }
        finally:
            stopped.set()
            thread.join(20)
            assert not thread.is_alive(), "Experimental view failed to unmount normally"
            if errors:
                raise errors[0]

    results["mounts"].append(mounted(False))
    # Two fresh mount processes and SQLite reopenings, no origin credentials or
    # network configured in the offline view. Same user-visible pathname.
    for _ in range(2):
        with StateStore(config) as store:
            OfflineController(config, store, None, settings).recover()
            assert store.row(copy.identity)["pinned"]
        results["mounts"].append(mounted(True))
    with StateStore(config) as store:
        controller = OfflineController(config, store, backend, settings)
        controller.unpin(args.path)
        controller.free(args.path)
        assert store.copy(copy.identity) is None
    after = backend.describe(args.path)
    remote_sha = hashlib.sha256()
    for block in backend.chunks(after):
        remote_sha.update(block)
    assert before.same_version(after)
    assert remote_sha.hexdigest() == expected
    assert not (config.storage_root / "local" / args.path).exists()
    results.update(
        pin_persisted_after_restart=True,
        local_eviction_preserves_remote_identity_revision_checksum=True,
        remote_sha256_after_eviction=remote_sha.hexdigest(),
        no_cloud_writes_or_deletes=True,
        experimental_view_unmounted=True,
    )
    args.result.write_text(json.dumps(results, indent=2) + "\n")
    os.chmod(args.result, 0o600)
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # No raw Graph URLs, item IDs or credential-bearing arguments in logs.
        print("M3 integration stopped; retain the private work directory for inspection.", file=sys.stderr)
        raise SystemExit(1) from None
