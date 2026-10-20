#!/usr/bin/env python3
"""Opt-in M6 extension of run_tracking, exclusively in its NEW disposable profile.

First run run_tracking.py in a new profile. This runner archives/exports its
three retained conflicts, retries its two live identities, then creates exactly
one 16 MiB file in that run's owned folder. Production remains download-only.
It never uses a historical M3/M4 profile. Config/logs/receipts remain private.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from cloudmarks.diagnostics import doctor
from cloudmarks.helper import OfflineStatusService
from cloudmarks.offline import OfflineController
from cloudmarks.offline_config import load_offline
from cloudmarks.onedrive import OneDrive
from cloudmarks.paths import SecureRoot
from cloudmarks.recovery import ConflictRecovery
from cloudmarks.state import ReadStore, StateStore
from cloudmarks.tracking import ChangeTracker
from cloudmarks.verification import verify
from cloudmarks.view import mount
from tests.integration.run_tracking import DisposableCloud


def offline_large(config, settings, path, expected, *, explicit):
    ready, stop = threading.Event(), threading.Event()
    errors = []

    def worker():
        try:
            mount(config, settings, offline=True, ready=ready.set, stop=stop.is_set)
        except Exception as error:
            errors.append(error)
            ready.set()

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    try:
        assert ready.wait(30) and not errors, "Offline view failed; consult private diagnostics"
        if explicit:
            with StateStore(config) as store:
                verify(OfflineController(config, store, None, settings), path)
        code = "import hashlib,pathlib,sys;print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())"
        digest = subprocess.check_output(
            [sys.executable, "-c", code, str(Path(settings["visible_root"]) / path)], text=True, timeout=30
        ).strip()
        assert digest == expected
        before = config.state_path.read_bytes()
        elapsed = []
        with ReadStore(config) as reader:
            service = OfflineStatusService({"visible_root": settings["visible_root"]}, reader, settings)
            # Any helper byte hashing would fail, independently of timing.
            original = SecureRoot.verify
            SecureRoot.verify = lambda *a, **kw: (_ for _ in ()).throw(
                AssertionError("Helper hashed content")
            )
            try:
                for _ in range(20):
                    start = time.monotonic()
                    result = service.lookup(path)
                    elapsed.append(time.monotonic() - start)
                    assert result["state"] == "kept_offline", result
            finally:
                SecureRoot.verify = original
        assert config.state_path.read_bytes() == before
        visible = Path(settings["visible_root"]) / path
        try:
            visible.open("r+b").close()
        except OSError as error:
            assert error.errno == 30
        else:
            raise AssertionError("Controlled view was writable")
        return {
            "explicit_full_hash": explicit,
            "bytes_read_sha256_matched": True,
            "helper_lookups": len(elapsed),
            "helper_max_seconds": round(max(elapsed), 4),
            "helper_rehashed_content": False,
            "helper_changed_database": False,
            "badge": "kept_offline",
            "write_rejected_EROFS": True,
        }
    finally:
        stop.set()
        thread.join(20)
        assert not thread.is_alive() and not errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--result", required=True, type=Path)
    args = parser.parse_args()
    config, settings = load_offline(args.config)
    base = Path(args.config).parent
    # This guard requires the explicit fresh M6 profile plus original-store baselines.
    assert base.name.startswith(".cloudmarks-m6-") and (base / "baseline.private.json").is_file()
    fixture = json.loads((base / "fixture.private.json").read_text())
    backend = OneDrive(config, settings)
    cloud = DisposableCloud(backend)
    cloud.prefix, cloud.owned = fixture["prefix"], fixture["owned"]
    folder = backend.describe(cloud.prefix, allow_directory=True)
    assert folder.remote.is_directory and cloud.prefix.startswith("cloudmarks-m4-")
    cloud.folder = folder.remote.identity.item_id
    for name in cloud.owned:
        cloud.verify(name)
    with StateStore(config) as store:
        control = OfflineController(config, store, backend, settings)
        tracker, recovery = ChangeTracker(control), ConflictRecovery(control)
        assert len(tracker.conflicts()) == 3 and not recovery.archives()
        receipts = []
        for name, retry in (("conflict.txt", True), ("update.txt", True), ("delete.txt", False)):
            receipt = recovery.resolve(cloud.prefix + "/" + name, retry_remote=retry)
            destination = base / ("export-" + receipt["archive_id"])
            recovery.export(receipt["archive_id"], destination)
            for label, source in receipt["sources"].items():
                if not source.get("missing"):
                    assert (
                        hashlib.sha256((destination / (label + ".bin")).read_bytes()).hexdigest()
                        == source["sha256"]
                    )
            receipts.append(receipt)
        assert not tracker.conflicts()
        jobs = tracker.resume()
        assert all(job["status"] == "done" for job in jobs)
        assert not (config.storage_root / "local" / cloud.prefix / "update.txt").exists()
        assert (config.storage_root / "local" / cloud.prefix / "renamed.txt").exists()
        assert not (config.storage_root / "local" / cloud.prefix / "delete.txt").exists()
    content = (b"M6 disposable larger-file trial.\n" * (16 * 1024 * 1024 // 32 + 1))[: 16 * 1024 * 1024]
    assert len(content) == 16 * 1024 * 1024
    # New unique owned folder; fail if an unexpected occupant appeared.
    assert "large.bin" not in cloud.owned
    value = cloud.request(
        "PUT",
        cloud.item_route(cloud.folder) + ":/large.bin:/content?@microsoft.graph.conflictBehavior=fail",
        content,
        binary=True,
    )
    cloud.owned["large.bin"] = value["id"]
    cloud.verify("large.bin")
    fixture["owned"] = cloud.owned
    (base / "fixture.private.json").write_text(json.dumps(fixture, indent=2) + "\n")
    os.chmod(base / "fixture.private.json", 0o600)
    large_path = cloud.prefix + "/large.bin"
    with StateStore(config) as store:
        tracker = ChangeTracker(OfflineController(config, store, backend, settings))
        tracker.queue_tree(cloud.prefix)
        assert all(job["status"] == "done" for job in tracker.resume())
        large = store.copy(tracker.identity_at(large_path))
        assert large.size == len(content) and large.sha256 == hashlib.sha256(content).hexdigest()
    scenes = [
        offline_large(config, settings, large_path, large.sha256, explicit=True),
        offline_large(config, settings, large_path, large.sha256, explicit=False),
    ]
    with ReadStore(config) as reader:
        report = doctor(OfflineController(config, reader, None, settings))
        assert report["ready_for_trial"] and report["counts"]["archives"] == 3
    baseline = json.loads((base / "baseline.private.json").read_text())
    for record in baseline.values():
        for path, digest in record["digests"].items():
            assert hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest
    results = {
        "milestone": "M6",
        "archives_exported_and_verified": 3,
        "live_conflicts_retried": 2,
        "unavailable_file_retired_locally": 1,
        "retained_versions_preserved": True,
        "remaining_issues": 0,
        "large_file_bytes": large.size,
        "large_file_server_checksum": backend.describe(large_path).kind,
        "offline_mount_scenes": scenes,
        "health": report,
        "m3_m4_bytes_unchanged": True,
        "views_stopped": True,
        "physical_disconnect_logout_tested": False,
        "daily_desktop_changed": False,
    }
    args.result.write_text(json.dumps(results, indent=2) + "\n")
    args.result.chmod(0o600)
    print(json.dumps(results))


if __name__ == "__main__":
    main()
