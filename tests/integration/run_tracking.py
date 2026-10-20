#!/usr/bin/env python3
"""Opt-in M4 disposable-cloud matrix; requires a NEW private store/config.

Unlike production (GET/download only), this test provisions a unique folder,
updates/renames files it created and deletes exactly one of those files with an
eTag precondition. Other fixtures and the containing allowed root are retained.
No daily mount, desktop session or personal account is changed.
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from cloudmarks.engine import StatusEngine
from cloudmarks.model import FileIdentity, StorageError
from cloudmarks.offline import OfflineController
from cloudmarks.offline_config import initialize, load_offline
from cloudmarks.onedrive import OneDrive
from cloudmarks.state import ReadStore, StateStore
from cloudmarks.tracking import ChangeTracker
from cloudmarks.view import mount, prove_view


class DisposableCloud:
    """Test-only writer; mutation authority is restricted to this run's IDs."""

    def __init__(self, backend):
        self.backend = backend
        self.folder = None
        self.owned = {}
        self.prefix = "cloudmarks-m4-" + uuid.uuid4().hex

    def request(self, method, route, payload, *, etag=None, binary=False):
        self.backend.authenticate()
        credential = json.loads(self.backend.validate_transport()["origin"]["token"])
        headers = {
            "Authorization": "Bearer " + credential["access_token"],
            "Content-Type": "application/octet-stream" if binary else "application/json",
        }
        if etag:
            headers["If-Match"] = etag
        data = payload if binary else (json.dumps(payload).encode() if payload is not None else None)
        request = urllib.request.Request(
            "https://graph.microsoft.com/v1.0/" + route, method=method, headers=headers, data=data
        )
        with self.backend.opener.open(request, timeout=30) as stream:
            data = stream.read(1024 * 1024 + 1)
            if len(data) > 1024 * 1024:
                raise StorageError("Fixture response exceeded limit")
            return json.loads(data) if data else None

    def item_route(self, item):
        q = urllib.parse.quote
        return f"drives/{q(self.backend.config.expected_drive_id, safe='')}/items/{q(item, safe='')}"

    def provision(self):
        value = self.request(
            "POST",
            self.item_route(self.backend.config.allowed_root_id) + "/children",
            {"name": self.prefix, "folder": {}, "@microsoft.graph.conflictBehavior": "fail"},
        )
        self.folder = value["id"]
        assert value["name"] == self.prefix and "folder" in value
        for name in ("update.txt", "conflict.txt", "delete.txt"):
            value = self.request(
                "PUT",
                self.item_route(self.folder)
                + ":/"
                + name
                + ":/content?@microsoft.graph.conflictBehavior=fail",
                ("M4 original " + name + "\n").encode() * 100,
                binary=True,
            )
            self.owned[name] = value["id"]
            self.verify(name)

    def verify(self, name):
        assert name in self.owned
        value = self.backend.describe(self.prefix + "/" + name)
        assert value.remote.identity == FileIdentity(self.backend.config.expected_drive_id, self.owned[name])
        parent = self.backend.item(self.owned[name])["parentReference"]
        assert parent["id"] == self.folder
        self.backend.verify_transport_item(value)
        return value

    def mutate(self, name, action):
        before = self.verify(name)
        route = self.item_route(self.owned[name])
        if action == "update":
            self.request(
                "PUT",
                route + "/content",
                b"M4 changed remote version.\n" * 200,
                binary=True,
                etag=before.remote.revision,
            )
        elif action == "rename":
            assert name == "update.txt"
            self.request("PATCH", route, {"name": "renamed.txt"}, etag=before.remote.revision)
            self.owned["renamed.txt"] = self.owned.pop(name)
            self.verify("renamed.txt")
        elif action == "delete":
            assert name == "delete.txt"  # No directory, recursive or arbitrary deletion.
            self.request("DELETE", route, None, etag=before.remote.revision)
            del self.owned[name]
        else:
            raise AssertionError("Unsupported fixture mutation")


def offline_read(config, settings, expected):
    ready, stopped = threading.Event(), threading.Event()
    errors = []

    def worker():
        try:
            mount(config, settings, offline=True, ready=ready.set, stop=stopped.is_set)
        except Exception as error:
            errors.append(error)
            ready.set()

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    try:
        assert ready.wait(30) and not errors
        for path, digest in expected.items():
            code = "import pathlib,hashlib,sys;print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())"
            actual = subprocess.check_output(
                [sys.executable, "-c", code, str(Path(settings["visible_root"]) / path)],
                timeout=20,
                text=True,
            ).strip()
            assert actual == digest
        with ReadStore(config) as reader:
            engine = StatusEngine(
                config, reader, read_path=lambda remote, copy: prove_view(config, settings, remote, copy)
            )
            folder = reader.connection.execute("SELECT drive_id,item_id FROM folder_intents").fetchone()
            # Updating a member invalidates the old manifest until explicit rescan.
            assert engine.status(FileIdentity(*folder)).state.value == "kept_offline"
        path = Path(settings["visible_root"]) / next(iter(expected))
        try:
            with path.open("r+b"):
                raise AssertionError("Writable offline view")
        except OSError as error:
            assert error.errno == 30
    finally:
        stopped.set()
        thread.join(20)
        assert not thread.is_alive() and not errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args()
    config, settings = load_offline(args.config)
    backend = OneDrive(config, settings)
    backend.authenticate()
    initialize(config, settings)  # Refuse adoption/reuse of any existing store.
    cloud = DisposableCloud(backend)
    fixture_record = Path(args.config).parent / "fixture.private.json"
    fixture_record.write_text(json.dumps({"prefix": cloud.prefix}) + "\n")
    fixture_record.chmod(0o600)
    cloud.provision()
    path = cloud.prefix + "/update.txt"
    conflict_path = cloud.prefix + "/conflict.txt"
    delete_path = cloud.prefix + "/delete.txt"
    with StateStore(config) as store:
        tracker = ChangeTracker(OfflineController(config, store, backend, settings))
        tracker.queue_tree(cloud.prefix)
        assert len(tracker.jobs()) == 3
    environment = dict(
        os.environ,
        HTTPS_PROXY="http://127.0.0.1:9",
        HTTP_PROXY="http://127.0.0.1:9",
        ALL_PROXY="http://127.0.0.1:9",
        NO_PROXY="",
    )
    command = [sys.executable, "-m", "cloudmarks", "offline", "--config", args.config, "resume"]
    subprocess.run(command, env=environment, capture_output=True, timeout=60)
    with StateStore(config) as store:
        tracker = ChangeTracker(OfflineController(config, store, backend, settings))
        assert all(job["status"] == "error" for job in tracker.jobs())
    result = subprocess.run(command, capture_output=True, timeout=360)
    (Path(args.config).parent / "worker.private.log").write_bytes(result.stdout + result.stderr)
    for _ in range(2):
        if result.returncode == 0:
            break
        result = subprocess.run(command, capture_output=True, timeout=360)
        with (Path(args.config).parent / "worker.private.log").open("ab") as log:
            log.write(result.stdout + result.stderr)
    assert result.returncode == 0
    with StateStore(config) as store:
        tracker = ChangeTracker(OfflineController(config, store, backend, settings))
        assert all(job["status"] == "done" for job in tracker.jobs())
        identity = tracker.identity_at(path)
        old = store.copy(identity)
        cloud.mutate("update.txt", "update")
        tracker.refresh(path)
        tracker.resume()
        assert all(job["status"] == "done" for job in tracker.jobs())
        assert store.copy(identity).revision != old.revision
        assert (config.storage_root / old.object_path).exists()
        tracker.queue_tree(cloud.prefix)
        tracker.resume()
        expected = {
            row["relative_path"]: store.copy(FileIdentity(row["drive_id"], row["item_id"])).sha256
            for row in store.connection.execute("SELECT * FROM local_entries")
        }
    for _ in range(2):
        offline_read(config, settings, expected)
    with StateStore(config) as store:
        tracker = ChangeTracker(OfflineController(config, store, backend, settings))
        original_conflict = (config.storage_root / "local" / conflict_path).read_bytes()
        local = config.storage_root / "local" / conflict_path
        local.chmod(0o600)
        local.write_bytes(b"M4 deliberate raw-store modification; preserve this local version.\n")
        cloud.mutate("conflict.txt", "update")
        tracker.refresh(conflict_path)
        tracker.resume()
        issue = next(value for value in tracker.conflicts() if value["retained_path"] == conflict_path)
        candidate = json.loads(issue["candidate"])
        assert (
            config.storage_root / candidate["object_path"]
        ).read_bytes() == b"M4 changed remote version.\n" * 200
        assert local.read_bytes().startswith(b"M4 deliberate")
        assert (config.storage_root / issue["original_object"]).read_bytes() == original_conflict
        cloud.mutate("update.txt", "rename")
        tracker.refresh(path)
        tracker.resume()
        assert (config.storage_root / "local" / path).exists()
        assert not (config.storage_root / "local" / cloud.prefix / "renamed.txt").exists()
        cloud.mutate("delete.txt", "delete")
        tracker.refresh(delete_path)
        assert store.remote(tracker.identity_at(delete_path)).deleted
        assert (config.storage_root / "local" / delete_path).exists()
        issues = tracker.conflicts()
        assert len(issues) == 3
        assert len(tracker.snapshots()) == 2
        assert len(tracker.changes()) >= 3
    # Public result excludes account, IDs, paths, tokens and raw adapter errors.
    results = {
        "scope": "new unique disposable folder in the verified dedicated test root",
        "created_files": 3,
        "remote_updates": 2,
        "remote_renames": 1,
        "remote_file_deletions_with_etag_precondition": 1,
        "production_transport_remote_writes": False,
        "rejecting_proxy_jobs_recovered_in_fresh_process": True,
        "clean_update_preserved_previous_object": True,
        "local_remote_conflict_preserved_three_versions": True,
        "rename_retained_original_path_and_remote_candidate": True,
        "remote_disappearance_retained_snapshot": True,
        "offline_mount_restarts_with_verified_folder_state": 2,
        "offline_view_write_rejected_EROFS": True,
        "remaining_conflicts_for_explicit_review": len(issues),
        "physical_network_disconnect_or_logout_or_disk_unplug_tested": False,
        "views_stopped": True,
    }
    args.result.write_text(json.dumps(results, indent=2) + "\n")
    os.chmod(args.result, 0o600)
    # Private handoff only; not part of the sanitized evidence copied to Git.
    private = Path(args.config).parent / "fixture.private.json"
    private.write_text(json.dumps({"prefix": cloud.prefix, "owned": cloud.owned}, indent=2) + "\n")
    private.chmod(0o600)
    print(json.dumps(results))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        import traceback

        print("M4 integration stopped; private work retained. Diagnostic frames:", file=sys.stderr)
        for frame in traceback.extract_tb(error.__traceback__):
            print(f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}", file=sys.stderr)
        print(type(error).__name__, file=sys.stderr)
        raise SystemExit(1) from None
