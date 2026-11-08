"""Drive observer loops deterministically; no metadata transport or desktop."""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from cloudmarks.activity import Activity, UnixHTTP, cache_entries, rc
from cloudmarks.folders import FolderObserver
from cloudmarks.model import (
    ConfigurationError,
    FileIdentity,
    IntegrityError,
    RemoteFile,
    ScopeError,
    StorageError,
)
from cloudmarks.onedrive import Observation


class ObserverWorkerTests(unittest.TestCase):
    def setUp(self):
        self.config = Mock(max_age_seconds=60, state_path=Path("/fake/state.sqlite3"))
        self.backend = Mock()
        self.settings = {"upload_root": "/fake/upload", "upload_socket": "/fake/socket", "version": 4}
        with patch.object(Activity, "validate_upload"):
            self.activity = Activity(self.config, {}, self.settings, backend=self.backend, clock=lambda: 100)
        self.remote = RemoteFile(FileIdentity("drive", "item"), "root", "folder/file", "r1", 1, 100)
        self.observation = Observation(self.remote, "sha256Hash", b"")

    def test_start_close_joins_both_workers(self):
        with patch.object(self.activity, "local_worker"), patch.object(self.activity, "remote_worker"):
            self.assertIs(self.activity.start(), self.activity)
            self.activity.close()
        self.assertEqual(len(self.activity.threads), 2)
        self.assertFalse(any(t.is_alive() for t in self.activity.threads))

    def test_remote_worker_refreshes_omissions_and_bounds_working_set(self):
        self.activity.interests = {"folder/file": 100, "omitted": 100}
        self.activity.remote = {
            "omitted": replace(self.remote, relative_path="omitted"),
            "old": replace(self.remote, relative_path="old"),
        }
        self.backend.describe_many.return_value = {"folder/file": self.observation, "omitted": None}
        with (
            patch("cloudmarks.activity.OBSERVED_PATHS", 1),
            patch.object(self.activity.stop, "wait", return_value=False),
            patch.object(self.activity.wake, "wait", side_effect=lambda delay: self.activity.stop.set()),
        ):
            self.activity.remote_worker()
        self.assertNotIn("omitted", self.activity.remote)
        self.assertEqual(list(self.activity.remote), ["folder/file"])
        self.assertEqual(len(self.backend.describe_many.call_args.args[0]), 1)

    def test_remote_failure_does_not_renew_old_evidence_and_backs_off(self):
        self.activity.interests = {"folder/file": 100}
        self.activity.remote = {"folder/file": self.remote}
        self.backend.describe_many.side_effect = TimeoutError("private diagnostic")

        def wait(delay):
            self.assertGreaterEqual(delay, 0)
            self.activity.stop.set()

        with patch.object(self.activity.stop, "wait", side_effect=wait):
            self.activity.remote_worker()
        self.assertEqual(self.activity.remote["folder/file"].observed_at, 100)
        self.activity.stop.clear()
        self.activity.interests.clear()
        with patch.object(self.activity.wake, "wait", side_effect=lambda delay: self.activity.stop.set()):
            self.activity.remote_worker()

    def test_remote_wrong_path_is_rejected(self):
        self.activity.interests = {"different": 100}
        self.backend.describe_many.return_value = {"different": self.observation}
        with patch.object(self.activity.stop, "wait", side_effect=lambda delay: self.activity.stop.set()):
            self.activity.remote_worker()
        self.assertNotIn("different", self.activity.remote)

    def test_local_worker_reuses_watch_and_processes_changed_and_removed_entries(self):
        watch = Mock()
        watch.changed.side_effect = [set(), {"gone", "new"}]
        stats = {"diskCache": {"path": "/fake/cache", "pathMeta": "/fake/meta"}}
        self.activity.interests = {"new": 100}
        count = [0]

        def wait(delay):
            count[0] += 1
            if count[0] == 2:
                self.activity.stop.set()

        with (
            patch.object(self.activity, "validate_upload"),
            patch(
                "cloudmarks.activity.mounted_view", return_value={"type": "fuse.rclone", "options": {"rw"}}
            ),
            patch("cloudmarks.activity.rc", side_effect=[stats, {"queue": []}, stats, {"queue": []}]),
            patch("cloudmarks.cache_watch.CacheWatch", return_value=watch) as factory,
            patch(
                "cloudmarks.activity.cache_entries",
                side_effect=[{"gone": ((1, 2, 3), False)}, {"new": ((1, 2, 3), True)}],
            ) as scan,
            patch.object(self.activity.stop, "wait", side_effect=wait),
        ):
            self.activity.local_worker()
        factory.assert_called_once()
        self.assertEqual(scan.call_count, 2)
        self.assertEqual(self.activity.local, {"new": "uploading"})
        self.assertNotIn("gone", self.activity.entries)
        watch.close.assert_called_once()

    def test_control_progress_requires_live_pid_freshness_scope_and_identity(self):
        candidate = {
            "version": 1,
            "operation": "free",
            "path": "folder",
            "active": True,
            "updated_at": 100,
            "start": "start",
            "pid": 123,
            "completed": 1,
            "total": 2,
            "phase": "freeing",
        }
        with (
            patch("cloudmarks.activity.read_private_json", return_value=candidate),
            patch("cloudmarks.controls.process_start", return_value="start"),
        ):
            self.activity.read_control_progress()
            self.assertEqual(self.activity.control_evidence("folder/file")["state"], "freeing")
            self.assertIsNone(self.activity.control_evidence("elsewhere"))
            candidate["updated_at"] = 50
            self.activity.read_control_progress()
            self.assertIsNone(self.activity.control_evidence("folder/file"))
        with patch("cloudmarks.activity.read_private_json", side_effect=FileNotFoundError):
            self.activity.read_control_progress()
        self.assertIsNone(self.activity.control_progress)

    def test_folder_request_lease_eviction_worker_errors_and_close(self):
        observer = FolderObserver(self.config, {}, Activity, backend=self.backend, clock=lambda: 100)
        self.assertEqual(observer.request("folder", "offline")["state"], "unknown")
        observer.results[("folder", "offline")] = (99, 101, Activity.result("kept_offline", "proof"))
        self.assertEqual(observer.request("folder", "offline")["state"], "kept_offline")
        for i in range(20):
            observer.request(str(i), "offline")
        self.assertLessEqual(len(observer.interests), 16)
        observer.interests = {("folder", "offline"): 100}
        observer.results.clear()
        for error, state in [(IntegrityError("bad"), "error"), (ScopeError("bad"), "unknown")]:
            observer.stop.clear()
            with (
                patch.object(observer, "check", side_effect=error),
                patch.object(observer.wake, "wait", side_effect=lambda delay: observer.stop.set()),
            ):
                observer.worker()
            self.assertEqual(observer.results[("folder", "offline")][2]["state"], state)
            observer.results.clear()
        observer.stop.clear()
        with (
            patch.object(observer, "check", return_value=(Activity.result("partial", "proof"), 101)),
            patch.object(observer.wake, "wait", side_effect=lambda delay: observer.stop.set()),
        ):
            observer.start()
            observer.thread.join(1)
            observer.close()
        self.assertFalse(observer.thread.is_alive())


class CacheAndTransportTests(unittest.TestCase):
    def test_nested_cache_only_reads_metadata_and_refuses_links_or_malformed_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            data, meta = base / "data", base / "meta"
            for root in (data, meta):
                (root / "nested").mkdir(parents=True)
            (data / "nested/file").write_text("content")
            info = meta / "nested/file"
            info.write_text(json.dumps({"Dirty": False}))
            self.assertFalse(cache_entries(data, meta)["nested/file"][1])
            for content in ("bad json", "{}", "[]", "x" * 17000):
                info.write_text(content)
                if content == "bad json":
                    self.assertTrue(cache_entries(data, meta)["nested/file"][1])
                else:
                    with self.assertRaises(StorageError):
                        cache_entries(data, meta)
            info.unlink()
            info.symlink_to(data / "nested/file")
            with self.assertRaises(OSError):
                cache_entries(data, meta)
            with patch("cloudmarks.activity.CACHE_ENTRIES", 0), self.assertRaises(StorageError):
                cache_entries(data, meta)

    def test_readonly_rc_response_limits_and_close_on_every_failure(self):
        for status, payload, success in [
            (200, b'{"queue":[]}', True),
            (500, b"{}", False),
            (200, b"[]", False),
            (200, b"x" * 20, False),
        ]:
            connection = Mock()
            connection.getresponse.return_value = SimpleNamespace(
                status=status, read=lambda n, payload=payload: payload
            )
            with (
                patch("cloudmarks.activity.UnixHTTP", return_value=connection),
                patch("cloudmarks.activity.MAX_RESPONSE", 16),
            ):
                if success:
                    self.assertEqual(rc("/fake/socket", "vfs/queue"), {"queue": []})
                else:
                    with self.assertRaises(StorageError):
                        rc("/fake/socket", "vfs/queue")
            connection.close.assert_called_once()

    def test_unix_http_requires_owned_socket_and_same_uid(self):
        import socket

        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            path = Path(temporary) / "socket"
            path.write_text("not a socket")
            connection = UnixHTTP(str(path))
            with self.assertRaises(ConfigurationError):
                connection.connect()
            path.unlink()
            with socket.socket(socket.AF_UNIX) as server:
                server.bind(str(path))
                server.listen()
                with patch("cloudmarks.activity.same_user", return_value=False):
                    try:
                        with self.assertRaises(ConfigurationError):
                            connection.connect()
                    finally:
                        connection.close()
