"""Automatic observations must never turn pending or stale bytes green."""

import json
import time
from dataclasses import replace
from unittest.mock import patch

from cloudmarks.activity import Activity, ObservedStore, cache_entries, rc
from cloudmarks.desktop import query_views
from cloudmarks.helper import OfflineStatusService
from cloudmarks.ipc import mapped_uri, validate_settings
from cloudmarks.model import ConfigurationError, FileIdentity, ScopeError, StorageError
from tests import test_offline, test_onedrive


class ActivityTests(test_offline.OfflineCase):
    def setUp(self):
        super().setUp()
        telemetry = patch("cloudmarks.activity.record")
        telemetry.start()
        self.addCleanup(telemetry.stop)
        self.now = [time.time()]
        self.desktop = {
            "version": 3,
            "mode": "onedrive",
            "enabled": True,
            "visible_root": self.settings["visible_root"],
            "core_config": str(self.private / "offline.json"),
            "socket": str(self.private / "status.sock"),
            "upload_root": str(self.root / "upload"),
            "upload_config": str(self.private / "upload.conf"),
            "upload_socket": str(self.private / "upload.sock"),
        }
        with patch.object(Activity, "validate_upload"):
            self.activity = Activity(
                self.config, self.settings, self.desktop, backend=self.backend, clock=lambda: self.now[0]
            )
        self.service = OfflineStatusService(self.desktop, self.store, self.settings, activity=self.activity)
        self.remote = replace(self.backend.describe(self.path).remote, observed_at=self.now[0])
        self.observe()
        self.activity.update_local({}, [])

    def observe(self, remote=None):
        self.activity.update_remote([replace(self.backend.describe(self.path), remote=remote or self.remote)])

    def state(self, view="upload"):
        return self.service.lookup_view(self.path, view)["state"]

    def test_untracked_cloud_file_is_automatic_without_pinning_or_db_writes(self):
        self.assertEqual(self.state(), "cloud_only")
        self.assertEqual(self.state("offline"), "cloud_only")
        self.assertIsNone(self.store.remote(self.identity))
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)

    def test_open_write_queue_cloud_confirmation_sequence(self):
        self.activity.update_local({self.path: ((1, 2, self.remote.size), True)}, [])
        self.assertEqual(self.state(), "uploading")
        queue = [{"name": self.path, "tries": 0, "uploading": False}]
        self.activity.update_local({self.path: ((1, 2, self.remote.size), True)}, queue)
        self.assertEqual(self.state(), "uploading")
        self.now[0] += 1
        self.activity.update_local({self.path: ((1, 2, self.remote.size), False)}, [])
        self.assertEqual(self.state(), "unknown")  # Queue empty is insufficient.
        self.now[0] += 1
        self.observe(replace(self.remote, observed_at=self.now[0]))
        self.assertEqual(self.state(), "cloud_only")

    def test_failed_retry_is_error_and_pending_never_green(self):
        self.keep()
        self.activity.update_local({}, [{"name": self.path, "tries": 2, "uploading": False}])
        self.assertEqual(self.state(), "error")
        self.assertEqual(self.state("offline"), "error")

    def test_stalled_local_worker_and_expired_remote_fail_closed(self):
        self.now[0] += 4
        self.assertEqual(self.state(), "unknown")

    def test_transient_queue_failure_keeps_only_the_original_live_lease(self):
        initial = self.activity.local_at
        with (
            patch.object(self.activity, "validate_upload"),
            patch(
                "cloudmarks.activity.mounted_view", return_value={"type": "fuse.rclone", "options": {"rw"}}
            ),
            patch("cloudmarks.activity.rc", side_effect=TimeoutError),
            patch.object(self.activity.stop, "wait", side_effect=lambda unused: self.activity.stop.set()),
        ):
            self.activity.local_worker()
        self.assertEqual(self.activity.local_at, initial)
        self.assertEqual(self.state(), "cloud_only")
        self.now[0] += 3.1
        self.assertEqual(self.state(), "unknown")
        self.activity.update_local({}, [])
        self.assertEqual(self.state(), "cloud_only")
        self.now[0] += 30
        self.activity.update_local({}, [])
        self.assertEqual(self.state(), "unknown")

    def test_evidence_lease_cannot_outlive_remote_confirmation(self):
        self.now[0] += 29.8
        self.activity.update_local({}, [])
        result = self.service.lookup_view(self.path, "upload")
        self.assertEqual(result["state"], "cloud_only")
        self.assertLessEqual(result["valid_for_ms"], 201)

    def test_evidence_lease_cannot_outlive_local_queue_confirmation(self):
        self.now[0] += 2.8
        result = self.service.lookup_view(self.path, "upload")
        self.assertEqual(result["state"], "cloud_only")
        self.assertLessEqual(result["valid_for_ms"], 201)

    def test_cloud_size_must_match_the_completed_local_write(self):
        self.activity.update_local({self.path: ((1, 2, self.remote.size + 1), False)}, [])
        self.now[0] += 1
        self.observe(replace(self.remote, observed_at=self.now[0]))
        self.assertEqual(self.state(), "unknown")

    def test_removed_cache_file_wakes_confirmation_and_invalidates_old_cloud_status(self):
        self.activity.update_local({self.path: ((1, 2, self.remote.size), False)}, [])
        self.now[0] += 1
        self.observe(replace(self.remote, observed_at=self.now[0]))
        self.assertEqual(self.state(), "cloud_only")
        self.activity.wake.clear()
        self.now[0] += 1
        self.activity.update_local({}, [])
        self.assertTrue(self.activity.wake.is_set())
        self.assertEqual(self.state(), "unknown")

    def test_local_copy_needs_real_proof_and_upload_view_never_claims_offline(self):
        self.keep()
        with patch("cloudmarks.view.attest_view", return_value=True):
            self.assertEqual(self.state("offline"), "kept_offline")
            self.assertEqual(self.state(), "cloud_only")
            self.visible().chmod(0o600)
            self.visible().write_bytes(b"tampered")
            self.assertEqual(self.state("offline"), "error")

    def test_new_observation_renews_evidence_without_modifying_database(self):
        self.keep()
        original = self.store.remote(self.identity)
        self.now[0] += 310
        self.activity.update_local({}, [])
        self.observe(replace(self.remote, observed_at=self.now[0]))
        with (
            patch("cloudmarks.verification.prove_view", return_value=True),
            patch("cloudmarks.engine.time.time", return_value=self.now[0]),
        ):
            # The engine's default clock was captured on import; use the same
            # explicitly controlled clock on this service.
            self.service.engine.clock = lambda: self.now[0]
            self.assertEqual(self.state("offline"), "kept_offline")
        self.assertEqual(self.store.remote(self.identity), original)

    def test_remote_revision_and_replacement_never_shadow_retained_copy(self):
        self.keep()
        before = self.visible().read_bytes()
        self.observe(replace(self.remote, revision="new-version"))
        self.assertEqual(self.state("offline"), "unknown")
        self.observe(
            replace(self.remote, identity=FileIdentity(self.config.expected_drive_id, "replacement"))
        )
        self.assertEqual(self.state("offline"), "unknown")
        self.assertEqual(self.visible().read_bytes(), before)

    def test_listing_omission_does_not_delete_or_expose_stale_green(self):
        self.keep()
        self.activity.update_remote([])
        self.assertEqual(self.state("offline"), "unknown")
        self.assertTrue(self.visible().exists())
        self.assertFalse(self.store.remote(self.identity).deleted)

    def test_future_observation_and_unverified_folders_stay_unknown(self):
        self.observe(replace(self.remote, observed_at=self.now[0] + 60))
        self.assertEqual(self.state(), "unknown")
        self.observe(replace(self.remote, is_directory=True, size=0))
        self.assertEqual(self.state(), "unknown")

    def test_catalog_validation_is_atomic_and_bounded(self):
        with self.assertRaises(StorageError):
            self.activity.update_remote([self.backend.describe(self.path)] * 1025)
        with self.assertRaises(ScopeError):
            self.activity.update_remote([self.backend.describe(self.path)] * 2)
        self.assertEqual(self.state(), "cloud_only")

    def test_bad_queue_cannot_grow_memory_or_escape_root(self):
        with self.assertRaises(StorageError):
            self.activity.update_local({}, [{}] * 10001)
        with self.assertRaises(ScopeError):
            self.activity.update_local({}, [{"name": "../escape", "tries": 0}])

    def test_metadata_overlay_is_read_only(self):
        self.keep()
        overlay = ObservedStore(self.store, self.activity)
        self.assertEqual(overlay.copy(self.identity), self.store.copy(self.identity))
        self.assertEqual(overlay.remote(self.identity), self.remote)

    def test_upload_config_account_and_folder_are_checked(self):
        path = self.private / "upload.conf"
        path.write_text(
            "[upload]\ntype=onedrive\ndrive_id=test-drive\nroot_folder_id=test-drive#allowed-root\n"
        )
        path.chmod(0o600)
        self.activity.validate_upload()
        path.write_text(path.read_text().replace("allowed-root", "another-root"))
        with self.assertRaises(ScopeError):
            self.activity.validate_upload()

    def test_cache_metadata_detects_open_writes_without_reading_contents(self):
        data, meta = self.root / "cache", self.root / "meta"
        data.mkdir()
        meta.mkdir()
        (data / "new.txt").write_bytes(b"test")
        self.assertTrue(cache_entries(data, meta)["new.txt"][1])
        (meta / "new.txt").write_text(json.dumps({"Dirty": False}))
        self.assertFalse(cache_entries(data, meta)["new.txt"][1])
        (data / "new.txt").unlink()
        (data / "new.txt").symlink_to(self.root / "outside")
        with self.assertRaises(ScopeError):
            cache_entries(data, meta)

    def test_two_roots_map_separately_and_overlap_is_rejected(self):
        validate_settings(self.desktop)
        self.assertEqual(
            mapped_uri("file://" + self.desktop["upload_root"] + "/a.txt", self.desktop), ("upload", "a.txt")
        )
        with self.assertRaises(ConfigurationError):
            validate_settings(dict(self.desktop, upload_root=self.desktop["visible_root"] + "/nested"))
        self.assertIsNone(mapped_uri("file:///etc/passwd", self.desktop))

    def test_ipc_has_no_mutations_and_selects_upload_view(self):
        response = self.service.handle({"version": 1, "op": "status", "paths": [self.path], "view": "upload"})
        self.assertEqual(response["results"][self.path]["state"], "cloud_only")
        with self.assertRaises(ValueError):
            self.service.handle({"version": 1, "op": "status", "paths": [self.path], "view": "evict"})
        with self.assertRaises(ScopeError):
            rc("/unused", "sync/copy")

    def test_same_filename_in_two_views_has_independent_results(self):
        def request(socket, paths, *, view="offline"):
            return {path: {"state": "uploading" if view == "upload" else "kept_offline"} for path in paths}

        with patch("cloudmarks.desktop.request", side_effect=request):
            result = query_views(self.desktop, {"a": ("offline", "a.txt"), "b": ("upload", "a.txt")})
        self.assertEqual(result[("offline", "a.txt")]["state"], "kept_offline")
        self.assertEqual(result[("upload", "a.txt")]["state"], "uploading")


class CatalogTests(test_onedrive.GraphTests):
    def catalog_graph(self, page):
        original = self.backend.graph
        self.backend.graph = lambda route: page if "/children" in route else original(route)

    def test_catalog_root_observes_without_download(self):
        self.catalog_graph({"value": [self.file]})
        values = self.backend.catalog()
        self.assertEqual([v.remote.relative_path for v in values], ["document.txt"])

    def test_catalog_reuses_only_verified_scan_parents_without_extra_graph_requests(self):
        folder = {
            "id": "nested",
            "name": "folder",
            "eTag": "v1",
            "folder": {},
            "parentReference": {"id": "allowed-root", "driveId": "test-drive"},
        }
        child = dict(self.file, parentReference={"id": "nested", "driveId": "test-drive"})
        original = self.backend.graph
        requests = []

        def graph(route):
            requests.append(route)
            if "/items/allowed-root/children" in route:
                return {"value": [folder]}
            if "/items/nested/children" in route:
                return {"value": [child]}
            return original(route)

        self.backend.graph = graph
        values = self.backend.catalog()
        self.assertEqual([v.remote.relative_path for v in values], ["folder", "folder/document.txt"])
        self.assertEqual(len(requests), 5)  # Account/root verification plus two folder listings.
        self.assertFalse(any(route.endswith("/items/nested") for route in requests))

    def test_catalog_does_not_enumerate_trash_or_skip_normal_hidden_folders(self):
        folders = [
            {
                "id": "f" + str(i),
                "name": name,
                "eTag": "v1",
                "folder": {},
                "parentReference": {"id": "allowed-root", "driveId": "test-drive"},
            }
            for i, name in enumerate((".Trash", ".Trash-1000", ".hidden"))
        ]
        original = self.backend.graph
        visited = []

        def graph(route):
            if "/items/allowed-root/children" in route:
                return {"value": folders}
            if "/children" in route:
                visited.append(route)
                return {"value": []}
            return original(route)

        self.backend.graph = graph
        self.assertEqual([v.remote.relative_path for v in self.backend.catalog()], [".hidden"])
        self.assertEqual(visited, ["drives/test-drive/items/f2/children?$top=128"])

    def test_ignored_trash_still_counts_toward_the_scan_bound(self):
        folders = [
            {
                "id": "f" + str(i),
                "name": ".Trash-" + str(i),
                "eTag": "v1",
                "folder": {},
                "parentReference": {"id": "allowed-root", "driveId": "test-drive"},
            }
            for i in range(3)
        ]
        self.catalog_graph({"value": folders})
        with self.assertRaises(StorageError):
            self.backend.catalog(limit=2)

    def test_catalog_rejects_scope_escape_pagination_duplicates_and_limit(self):
        for page in (
            {"value": [self.file], "@odata.nextLink": "https://evil.invalid/steal"},
            {"value": [self.file, self.file]},
            {"value": [dict(self.file, remoteItem={})]},
        ):
            self.catalog_graph(page)
            with self.assertRaises(ScopeError):
                self.backend.catalog()
        self.catalog_graph({"value": [self.file] * 2})
        with self.assertRaises(StorageError):
            self.backend.catalog(limit=1)
