"""User controls must preserve bytes, pins, cloud content and mount ownership."""

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from cloudmarks.controls import binding, execute, profile
from cloudmarks.model import ConfigurationError, IntegrityError, ScopeError, StorageError
from cloudmarks.offline import OfflineController
from cloudmarks.onedrive import Cancelled
from tests import test_tracking


class ControlsTests(test_tracking.TrackingCase):
    def setUp(self):
        super().setUp()
        self.store.close()
        self.mounted = True
        self.stops = self.starts = 0
        self.events = []
        owner = self

        class Service:
            def stop(self):
                owner.stops += 1
                owner.mounted = False

            def start(self):
                owner.starts += 1
                owner.mounted = True

        self.service = Service()
        self.desktop = {
            "version": 4,
            "mode": "onedrive",
            "visible_root": self.settings["visible_root"],
            "core_config": "/unused/offline.json",
            "upload_root": str(self.root / "upload"),
            "control_config": str(self.private / "controls.json"),
        }
        self.control = {"version": 1, "view_service": "cloudmarks-test-view.service"}
        p = Path(self.desktop["control_config"])
        p.write_text(json.dumps(self.control))
        p.chmod(0o600)
        self.desktop["control_binding"] = binding(self.config, self.desktop, self.control)
        original = self.backend.describe
        self.backend.describe = lambda path, **kwargs: original(path)

        def controller(*args, **kwargs):
            def unmounted(settings):
                if self.mounted:
                    raise StorageError("View still mounted")

            return OfflineController(*args, **kwargs, volume_check=lambda *a: None, unmounted=unmounted)

        for patcher in (
            patch("cloudmarks.controls.load_offline", return_value=(self.config, self.settings)),
            patch(
                "cloudmarks.controls.mounted_view",
                side_effect=lambda *a: {"type": "fuse.rclone"} if self.mounted else None,
            ),
            patch("cloudmarks.controls.OfflineController", side_effect=controller),
            patch("cloudmarks.verification.attest_view", return_value=True),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_control(self, operation, path="folder", **kwargs):
        return execute(
            self.desktop,
            path,
            operation,
            backend=self.backend,
            service=self.service,
            emit=self.events.append,
            **kwargs,
        )

    def test_dry_run_changes_no_bytes_pins_progress_or_mounts(self):
        result = self.run_control("keep", dry_run=True)
        self.assertTrue(result["directory"])
        self.assertEqual(result["files"], 2)
        self.assertFalse(result["cloud_writes"])
        self.assertEqual((self.stops, self.starts), (0, 0))
        self.assertFalse((self.private / "control-progress.json").exists())
        self.reopen()
        self.assertIsNone(self.store.copy(self.identity))

    def test_folder_keep_then_free_preserves_cloud_and_restores_view(self):
        before = dict(self.backend.records)
        result = self.run_control("keep")
        self.assertEqual(result["completed"], 2)
        self.assertTrue(self.mounted)
        self.assertTrue((self.disk / "local/folder/disposable.txt").exists())
        result = self.run_control("free")
        self.assertEqual(result["completed"], 2)
        self.assertTrue(self.mounted)
        self.assertFalse((self.disk / "local/folder").exists())
        self.assertEqual(self.backend.records, before)
        self.assertEqual((self.stops, self.starts), (2, 2))
        self.assertEqual(self.events[-1]["phase"], "complete")

    def test_file_keep_free_and_free_again_are_safe(self):
        self.run_control("keep", self.path)
        self.run_control("free", self.path)
        result = self.run_control("free", self.path)
        self.assertTrue(result["already_cloud_only"])
        self.assertEqual(self.backend.records["file"][0], self.path)

    def test_other_direct_pins_survive_freeing_a_folder(self):
        self.run_control("keep")
        self.reopen()
        self.store.set_pin(self.identity, True)
        self.store.close()
        result = self.run_control("free")
        self.assertEqual(result["retained_by_other_pins"], 1)
        self.assertTrue(self.visible().exists())

    def test_empty_folders_are_retained_and_released(self):
        self.backend.records = {"folder": ("folder", b"", "d1", True)}
        self.assertEqual(self.run_control("keep")["completed"], 0)
        self.assertTrue((self.disk / "local/folder").is_dir())
        self.run_control("free")
        self.assertFalse((self.disk / "local/folder").exists())

    def test_busy_view_prevents_all_content_changes(self):
        def busy():
            raise StorageError("Close the test view and retry")

        self.service.stop = busy
        with self.assertRaises(StorageError):
            self.run_control("keep")
        self.assertFalse(self.visible().exists())
        self.assertTrue(self.mounted)

    def test_cancellation_restores_the_original_view_and_keeps_pending_intent(self):
        stopped = [False]
        self.backend.hook = lambda: stopped.__setitem__(0, True)
        with self.assertRaises(Cancelled):
            self.run_control("keep", cancelled=lambda: stopped[0])
        self.assertTrue(self.mounted)
        self.assertEqual(self.events[-1]["phase"], "cancelled")
        self.reopen()
        self.assertTrue(self.store.row(self.identity)["pinned"])

    def test_changed_cloud_membership_prevents_free_before_unpinning(self):
        self.run_control("keep")
        self.backend.records["new"] = ("folder/new.txt", b"new", "v1", False)
        with self.assertRaises(ScopeError):
            self.run_control("free")
        self.assertTrue(self.visible().exists())
        self.reopen()
        self.assertTrue(self.store.row(self.identity)["pinned"])

    def test_corrupted_local_file_is_not_freed(self):
        self.run_control("keep")
        self.visible().chmod(0o600)
        self.visible().write_bytes(b"changed")
        with self.assertRaises(IntegrityError):
            self.run_control("free")
        self.assertEqual(self.visible().read_bytes(), b"changed")
        self.assertTrue(self.mounted)

    def test_stale_profile_binding_and_unsafe_service_names_are_rejected(self):
        with self.assertRaises(ScopeError):
            self.run_control("keep", expected_binding="b" * 64)
        p = Path(self.desktop["control_config"])
        p.write_text(json.dumps(dict(self.control, view_service="unrelated.service")))
        with self.assertRaises(ConfigurationError):
            profile(self.desktop)

    def test_cli_help_invalid_action_and_machine_readable_dry_run_flag(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [sys.executable, "-m", "cloudmarks", "control", "--help"],
            cwd=root,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("--dry-run", result.stdout)
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "cloudmarks",
                "control",
                "--settings",
                "unused",
                "--path",
                "x",
                "--operation",
                "delete-cloud",
            ],
            cwd=root,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 2)

    def folder_observer(self):
        from types import SimpleNamespace

        from cloudmarks.activity import Activity
        from cloudmarks.folders import FolderObserver

        return FolderObserver(
            self.config, self.settings, SimpleNamespace(result=Activity.result), backend=self.backend
        )

    def test_folder_badge_requires_complete_fresh_membership_and_local_bytes(self):
        self.run_control("keep")
        observer = self.folder_observer()
        with patch("cloudmarks.folders.attest_view", return_value=True):
            self.assertEqual(observer.check("folder", "offline")[0]["state"], "kept_offline")
            self.backend.records["new"] = ("folder/new.txt", b"new", "v1", False)
            observer.trees.clear()
            result = observer.check("folder", "offline")[0]
            self.assertEqual(result["state"], "partial")
            self.assertIn("2/3", result["reason"])
            self.assertEqual(observer.check("folder", "upload")[0]["state"], "cloud_only")
        # A changed inode/fingerprint must force rehash rather than reuse green.
        self.visible().chmod(0o600)
        self.visible().write_bytes(b"changed")
        with patch("cloudmarks.folders.attest_view", return_value=True), self.assertRaises(IntegrityError):
            observer.check("folder", "offline")

    def test_empty_folder_badge_and_memory_only_request(self):
        self.backend.records = {"folder": ("folder", b"", "d1", True)}
        self.run_control("keep")
        observer = self.folder_observer()
        with patch("cloudmarks.folders.attest_view", return_value=True):
            self.assertEqual(observer.check("folder", "offline")[0]["state"], "kept_offline")
        with (
            patch("cloudmarks.folders.ReadStore", side_effect=AssertionError("UI disk access")),
            patch.object(self.backend, "tree", side_effect=AssertionError("UI network")),
        ):
            self.assertEqual(observer.request("folder", "offline")["state"], "unknown")

    def test_more_than_128_files_keep_badge_index_restart_and_free(self):
        self.backend.records = {"folder": ("folder", b"", "d1", True)} | {
            f"item-{i}": (f"folder/file-{i}.txt", b"tiny test\n", "v1", False) for i in range(140)
        }
        self.assertEqual(self.run_control("keep")["completed"], 140)
        observer = self.folder_observer()
        with patch("cloudmarks.folders.attest_view", return_value=True):
            result = observer.check("folder", "offline")[0]
            self.assertEqual(result["state"], "kept_offline")
            self.assertIn("140", result["reason"])
        self.reopen()
        from cloudmarks.tracking import ChangeTracker

        ChangeTracker(self.controller).check_online()
        plan = self.store.connection.execute(
            "EXPLAIN QUERY PLAN SELECT item_id FROM local_entries WHERE cm_path_key(relative_path)=?",
            (self.config.path_key("folder/file-139.txt"),),
        ).fetchone()
        self.assertIn("local_entries_path_key", str(tuple(plan)))
        self.store.close()
        self.assertEqual(self.run_control("free")["completed"], 140)

    def test_failed_free_restores_pin_intent_and_preserves_unfreed_bytes(self):
        self.run_control("keep")
        with (
            patch.object(
                OfflineController, "free", side_effect=StorageError("Artificial interrupted eviction")
            ),
            self.assertRaises(StorageError),
        ):
            self.run_control("free")
        self.assertTrue(self.mounted)
        self.assertTrue(self.visible().exists())
        self.reopen()
        self.assertTrue(self.store.row(self.identity)["pinned"])
        self.assertEqual(self.store.connection.execute("SELECT pinned FROM folder_intents").fetchone()[0], 1)
