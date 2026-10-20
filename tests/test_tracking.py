"""M4 change/recovery matrix, using disposable artificial cloud data."""

import hashlib
import json
import os
import sqlite3
import subprocess
import time
import unittest
import urllib.error
from dataclasses import replace
from unittest.mock import patch

from cloudmarks.engine import StatusEngine
from cloudmarks.model import FileIdentity, IntegrityError, RemoteFile, ScopeError, State, StorageError
from cloudmarks.onedrive import Cancelled, Observation, RemoteMissing
from cloudmarks.state import ReadStore
from cloudmarks.tracking import ChangeTracker
from tests import test_onedrive as graph_fixtures
from tests.test_offline import OfflineCase


class Catalog:
    def __init__(self, config):
        self.config = config
        self.records = {
            "folder": ("folder", b"", "d1", True),
            "file": ("folder/disposable.txt", b"Original file.\n" * 100, "v1", False),
            "nested": ("folder/nested", b"", "d1", True),
            "other": ("folder/nested/other.txt", b"Other file.\n" * 100, "v1", False),
        }
        self.fail = self.missing = self.outside = False
        self.hook = lambda: None
        self.tree_hook = lambda count: None
        self.trees = 0
        self.reads = 0

    def value(self, item):
        path, content, revision, directory = self.records[item]
        remote = RemoteFile(
            FileIdentity(self.config.expected_drive_id, item),
            self.config.allowed_root_id,
            path,
            revision,
            len(content),
            time.time(),
            None if directory else hashlib.sha256(content).hexdigest(),
            is_directory=directory,
        )
        return Observation(
            remote, "" if directory else "sha256Hash", b"" if directory else hashlib.sha256(content).digest()
        )

    def describe(self, path):
        if self.fail:
            raise StorageError("Artificial network failure with private adapter details")
        for item, value in self.records.items():
            if value[0] == path:
                return self.value(item)
        raise RemoteMissing("Artificial missing path")

    def describe_identity(self, identity):
        if self.fail:
            raise StorageError("Artificial network failure")
        if self.missing or identity.item_id not in self.records:
            raise RemoteMissing("Artificial missing stable ID")
        if self.outside:
            raise ScopeError("Artificial move outside allowed root")
        return self.value(identity.item_id)

    def chunks(self, observation, *, cancelled=lambda: False):
        self.reads += 1
        self.hook()
        yield self.records[observation.remote.identity.item_id][1]

    def tree(self, path, **kwargs):
        self.trees += 1
        self.tree_hook(self.trees)
        if self.fail:
            raise StorageError("Incomplete tree")
        return [
            self.value(item)
            for item, value in self.records.items()
            if value[0] == path or value[0].startswith(path + "/")
        ]

    def verify_transport_item(self, observation):
        pass


class TrackingCase(OfflineCase):
    def setUp(self):
        super().setUp()
        self.backend = Catalog(self.config)
        self.controller.backend = self.backend
        self.tracker = ChangeTracker(self.controller)
        self.path = "folder/disposable.txt"
        self.identity = FileIdentity(self.config.expected_drive_id, "file")

    def change(self, *, path=None, content=None, revision="v2"):
        old = self.backend.records["file"]
        self.backend.records["file"] = (
            path or old[0],
            content if content is not None else b"Remote v2.\n" * 100,
            revision,
            False,
        )

    def state(self):
        return self.tracker.jobs()[0]["status"]

    def candidate_bytes(self):
        record = json.loads(self.tracker.conflicts()[0]["candidate"])
        return (self.disk / record["object_path"]).read_bytes()


class ChangeTests(TrackingCase):
    def test_network_loss_retains_durable_intent_and_resume_after_restart(self):
        self.tracker.queue(self.path)
        self.backend.fail = True
        self.tracker.resume()
        self.assertEqual(self.state(), "error")
        self.assertTrue(self.store.row(self.identity)["pinned"])
        self.assertFalse(self.visible().exists())
        self.reopen()
        self.tracker = ChangeTracker(self.controller)
        self.backend.fail = False
        self.tracker.resume()
        self.assertEqual(self.state(), "done")
        self.assertEqual(self.visible().read_bytes(), self.backend.records["file"][1])

    def test_running_job_is_pending_after_abrupt_worker_death(self):
        self.tracker.queue(self.path)
        self.store.close()
        program = """
import os,sys
from pathlib import Path
from cloudmarks.config import Config
from cloudmarks.state import StateStore
root=Path(sys.argv[1])
config=Config('test-drive','allowed-root',('blocked-personal',),root/'disk','test-volume',root/'state/state.sqlite3',case_sensitive=True)
store=StateStore(config)
store.connection.execute("UPDATE jobs SET status='running'")
os._exit(73)
"""
        result = subprocess.run([os.sys.executable, "-c", program, str(self.root)], capture_output=True)
        self.assertEqual(result.returncode, 73)
        self.reopen()
        self.tracker = ChangeTracker(self.controller)
        self.assertEqual(self.state(), "pending")
        self.tracker.resume()
        self.assertEqual(self.state(), "done")

    def test_cancel_mid_download_preserves_old_bytes_and_job_can_resume(self):
        old = self.keep()
        self.change()
        self.tracker.queue(self.path)
        cancelled = [False]
        self.controller.cancelled = lambda: cancelled[0]
        self.backend.hook = lambda: cancelled.__setitem__(0, True)
        with self.assertRaises(Cancelled):
            self.tracker.resume()
        self.assertEqual(self.state(), "pending")
        self.assertEqual(self.store.copy(self.identity), old)
        self.assertEqual(self.visible().read_bytes(), (self.disk / old.object_path).read_bytes())
        self.controller.cancelled = lambda: False
        self.backend.hook = lambda: None
        self.tracker.resume()
        self.assertEqual(self.state(), "done")

    def test_clean_remote_revision_replaces_view_but_retains_original_and_history(self):
        old = self.keep()
        original = self.visible().read_bytes()
        self.change()
        self.tracker.refresh(self.path)
        self.assertEqual(self.state(), "pending")
        self.assertEqual(self.visible().read_bytes(), original)
        self.tracker.resume()
        self.assertEqual(self.state(), "done")
        self.assertEqual(self.visible().read_bytes(), self.backend.records["file"][1])
        self.assertEqual((self.disk / old.object_path).read_bytes(), original)
        self.assertEqual(self.store.connection.execute("SELECT kind FROM changes").fetchone()[0], "revision")

    def test_current_job_reuses_verified_bytes_and_refreshes_evidence(self):
        old = self.keep()
        before = self.backend.reads
        self.store.fail_transfer(self.identity, "Earlier interrupted attempt")
        self.tracker.queue(self.path)
        self.tracker.resume()
        self.assertEqual(self.state(), "done")
        self.assertEqual(self.backend.reads, before)
        self.assertEqual(self.store.copy(self.identity), old)
        self.assertEqual(self.store.row(self.identity)["operation"], "idle")
        self.change(revision="v1")  # A matching revision alone is insufficient.
        self.tracker.queue(self.path)
        self.tracker.resume()
        self.assertGreater(self.backend.reads, before)
        self.assertEqual(self.visible().read_bytes(), self.backend.records["file"][1])

    def test_remote_rename_keeps_original_path_and_verified_remote_candidate(self):
        old = self.keep()
        original = self.visible().read_bytes()
        self.change(path="folder/renamed.txt")
        self.tracker.refresh(self.path)
        self.tracker.resume()
        self.assertEqual(self.state(), "blocked")
        self.assertEqual(self.visible().read_bytes(), original)
        self.assertFalse((self.disk / "local/folder/renamed.txt").exists())
        self.assertEqual(self.candidate_bytes(), self.backend.records["file"][1])
        self.assertEqual(self.store.copy(self.identity), old)
        engine = StatusEngine(self.config, self.store, read_path=lambda *args: True)
        self.assertEqual(engine.status(self.identity).state, State.ERROR)
        before = self.backend.reads
        self.tracker.queue(self.path)
        self.tracker.resume()
        self.assertEqual(self.backend.reads, before)  # Verified candidate is reused.
        with self.assertRaises(ScopeError):
            self.controller.keep("folder/renamed.txt")
        with self.assertRaises(ScopeError):
            self.tracker.check_online()

    def test_unavailable_stable_id_is_not_eviction_evidence(self):
        old = self.keep()
        self.backend.missing = True
        self.tracker.refresh(self.path)
        self.assertTrue(self.store.remote(self.identity).deleted)
        self.assertIn("inaccessible", self.tracker.conflicts()[0]["reason"])
        self.assertEqual(self.store.copy(self.identity), old)
        self.controller.unpin(self.path)
        with self.assertRaises(ScopeError):
            self.controller.free(self.path)
        self.assertTrue(self.visible().exists())

    def test_failed_metadata_or_out_of_scope_move_never_means_deleted(self):
        self.keep()
        for attribute in ("fail", "outside"):
            setattr(self.backend, attribute, True)
            with self.assertRaises((StorageError, ScopeError)):
                self.tracker.refresh(self.path)
            self.assertFalse(self.store.remote(self.identity).deleted)
            self.assertTrue(self.visible().exists())
            setattr(self.backend, attribute, False)

    def test_local_and_remote_edits_preserve_all_three_versions(self):
        old = self.keep()
        original = self.visible().read_bytes()
        self.visible().chmod(0o600)
        self.visible().write_bytes(b"Unsent raw-store local change")
        self.change()
        self.tracker.queue(self.path)
        self.tracker.resume()
        self.assertEqual(self.state(), "blocked")
        self.assertTrue(self.store.row(self.identity)["dirty"])
        self.assertEqual(self.visible().read_bytes(), b"Unsent raw-store local change")
        self.assertEqual((self.disk / old.object_path).read_bytes(), original)
        self.assertEqual(self.candidate_bytes(), self.backend.records["file"][1])
        self.tracker.resume()  # A blocked job never retries implicitly.
        self.assertEqual(self.visible().read_bytes(), b"Unsent raw-store local change")

    def test_changed_conflict_candidate_retains_prior_candidate_in_snapshot_index(self):
        self.keep()
        self.store.set_dirty(self.identity, True)
        self.tracker.queue(self.path)
        self.tracker.resume()
        first = self.tracker.snapshots()[0]
        self.change(revision="v1")  # Same eTag with changed checksum must not reuse bytes.
        self.tracker.queue(self.path)
        self.tracker.resume()
        self.assertEqual(len(self.tracker.snapshots()), 2)
        self.assertTrue((self.disk / first["object_path"]).exists())
        self.assertEqual(self.candidate_bytes(), self.backend.records["file"][1])

    def test_local_deletion_is_not_silently_restored(self):
        old = self.keep()
        self.visible().unlink()
        self.tracker.queue(self.path)
        self.tracker.resume()
        self.assertEqual(self.state(), "blocked")
        self.assertFalse(self.visible().exists())
        self.assertTrue((self.disk / old.object_path).exists())
        self.assertEqual(self.candidate_bytes(), self.backend.records["file"][1])

    def test_external_local_rename_is_preserved_and_not_automatically_recreated(self):
        self.keep()
        renamed = self.visible().with_name("externally-renamed.txt")
        self.visible().rename(renamed)
        original = renamed.read_bytes()
        self.tracker.queue(self.path)
        self.tracker.resume()
        self.assertEqual(self.state(), "blocked")
        self.assertFalse(self.visible().exists())
        self.assertEqual(renamed.read_bytes(), original)
        self.assertIsNotNone(self.tracker.conflicts()[0]["candidate"])

    def test_reused_remote_path_never_changes_the_queued_stable_identity(self):
        old = self.keep()
        del self.backend.records["file"]
        self.backend.records["replacement"] = (self.path, b"Unrelated replacement", "v1", False)
        self.tracker.queue(self.path)
        self.tracker.resume()
        self.assertEqual(self.state(), "blocked")
        self.assertEqual(self.store.copy(self.identity), old)
        self.assertNotEqual(self.visible().read_bytes(), b"Unrelated replacement")
        self.assertIsNone(self.store.row(FileIdentity(self.config.expected_drive_id, "replacement")))

    def test_changing_remote_conflict_candidate_is_never_indexed_as_complete(self):
        self.keep()
        self.store.set_dirty(self.identity, True)
        self.tracker.queue(self.path)
        self.backend.hook = lambda: self.change()
        self.tracker.resume()
        self.assertEqual(self.state(), "error")
        self.assertEqual(self.tracker.snapshots(), [])
        self.assertIsNone(self.tracker.conflicts()[0]["candidate"])
        self.backend.hook = lambda: None
        self.tracker.resume()
        self.assertEqual(self.state(), "blocked")
        self.assertEqual(self.candidate_bytes(), self.backend.records["file"][1])

    def test_explicit_dirty_flag_prevents_overwrite_even_when_bytes_match(self):
        self.keep()
        self.store.set_dirty(self.identity, True)
        self.tracker.queue(self.path)
        self.tracker.resume()
        self.assertEqual(self.state(), "blocked")
        self.assertIsNotNone(self.tracker.conflicts()[0]["candidate"])

    def test_disk_disconnect_does_not_consume_queued_intent(self):
        self.tracker.queue(self.path)
        self.controller.volume_check = lambda *args: (_ for _ in ()).throw(StorageError("Disk missing"))
        with self.assertRaises(StorageError):
            self.tracker.resume()
        self.assertEqual(self.state(), "pending")
        self.controller.volume_check = lambda *args: None
        self.tracker.resume()
        self.assertEqual(self.state(), "done")

    def test_conflict_disk_budget_failure_preserves_original_then_retry_captures_remote(self):
        self.keep()
        self.store.set_dirty(self.identity, True)
        self.tracker.queue(self.path)
        self.settings["budget_bytes"] = 1
        self.tracker.resume()
        self.assertEqual(self.state(), "error")
        self.assertIsNone(self.tracker.conflicts()[0]["candidate"])
        self.settings["budget_bytes"] = 32 * 1024 * 1024
        self.tracker.resume()
        self.assertEqual(self.state(), "blocked")
        self.assertIsNotNone(self.tracker.conflicts()[0]["candidate"])

    def test_unpin_cancels_pending_job_without_eviction_or_cloud_change(self):
        self.keep()
        original = dict(self.backend.records)
        self.tracker.queue(self.path)
        self.controller.unpin(self.path)
        self.assertEqual(self.state(), "cancelled")
        self.assertTrue(self.visible().exists())
        self.tracker.resume()
        self.assertEqual(self.state(), "cancelled")
        self.assertEqual(self.backend.records, original)

    def test_readonly_inspection_never_recovers_running_jobs(self):
        self.tracker.queue(self.path)
        self.store.connection.execute("UPDATE jobs SET status='running'")
        before = self.config.state_path.read_bytes()
        with ReadStore(self.config) as reader:
            tracker = ChangeTracker(
                type("Control", (), {"config": self.config, "store": reader, "backend": None})()
            )
            self.assertEqual(tracker.jobs()[0]["status"], "running")
            self.assertEqual(tracker.conflicts(), [])
            with self.assertRaises(sqlite3.OperationalError):
                reader.connection.execute("UPDATE jobs SET status='pending'")
        self.assertEqual(before, self.config.state_path.read_bytes())

    def test_online_mount_check_requires_update_before_local_shadowing(self):
        self.keep()
        self.change()
        with self.assertRaises(ScopeError):
            self.tracker.check_online()
        self.assertEqual(self.state(), "pending")
        self.tracker.resume()
        self.tracker.check_online()


class FolderTests(TrackingCase):
    def folder_identity(self):
        return FileIdentity(self.config.expected_drive_id, "folder")

    def engine(self):
        def prove(remote, copy):
            return (self.disk / "local" / remote.relative_path).read_bytes() == (
                self.disk / copy.object_path
            ).read_bytes()

        return StatusEngine(self.config, self.store, read_path=prove)

    def test_complete_manifest_queues_only_then_worker_verifies_every_file(self):
        self.tracker.queue_tree("folder")
        self.assertEqual(self.backend.reads, 0)
        self.assertEqual(len(self.tracker.jobs()), 2)
        self.assertEqual(self.engine().status(self.folder_identity()).state, State.UNKNOWN)
        self.reopen()
        self.tracker = ChangeTracker(self.controller)
        self.tracker.resume()
        self.assertTrue(all(job["status"] == "done" for job in self.tracker.jobs()))
        self.assertEqual(self.engine().status(self.folder_identity()).state, State.KEPT_OFFLINE)
        self.assertTrue(self.store.row(FileIdentity(self.config.expected_drive_id, "other"))["pinned"])

    def test_recursive_unpin_preserves_direct_pins_and_downloaded_bytes(self):
        self.keep()  # Direct intent, independent of folder intent.
        self.tracker.queue_tree("folder")
        self.tracker.resume()
        self.tracker.unpin_tree("folder")
        self.assertTrue(self.store.row(self.identity)["pinned"])
        self.assertFalse(self.store.row(FileIdentity(self.config.expected_drive_id, "other"))["pinned"])
        self.assertTrue(self.visible().exists())
        self.controller.unpin(self.path)
        self.assertFalse(self.store.row(self.identity)["pinned"])
        self.assertEqual(self.engine().status(self.folder_identity()).state, State.SAVED_LOCALLY)

    def test_overlapping_folder_intents_survive_unpinning_parent(self):
        self.tracker.queue_tree("folder")
        self.tracker.queue_tree("folder/nested")
        self.tracker.unpin_tree("folder")
        other = FileIdentity(self.config.expected_drive_id, "other")
        self.assertTrue(self.store.row(other)["pinned"])
        self.assertFalse(self.store.row(self.identity)["pinned"])
        self.tracker.unpin_tree("folder/nested")
        self.assertFalse(self.store.row(other)["pinned"])

    def test_changed_or_incomplete_listing_preserves_previous_manifest_and_intent(self):
        self.tracker.queue_tree("folder")
        before = tuple(self.store.connection.execute("SELECT * FROM folder_intents").fetchone())
        self.backend.tree_hook = lambda count: self.change() if count == 4 else None
        with self.assertRaises(IntegrityError):
            self.tracker.queue_tree("folder")
        self.assertEqual(
            tuple(self.store.connection.execute("SELECT * FROM folder_intents").fetchone()), before
        )
        self.backend.fail = True
        with self.assertRaises(StorageError):
            self.tracker.queue_tree("folder")
        self.assertEqual(
            tuple(self.store.connection.execute("SELECT * FROM folder_intents").fetchone()), before
        )

    def test_listing_omission_never_deletes_existing_local_snapshot(self):
        self.tracker.queue_tree("folder")
        self.tracker.resume()
        original = self.visible().read_bytes()
        del self.backend.records["file"]
        self.tracker.queue_tree("folder")
        self.assertFalse(self.store.remote(self.identity).deleted)
        self.assertEqual(self.visible().read_bytes(), original)
        self.assertFalse(self.store.row(self.identity)["pinned"])

    def test_failed_manifest_commit_rolls_back_observations_membership_and_jobs(self):
        original = self.store.recompute_pin
        calls = [0]

        def fail(*args):
            calls[0] += 1
            if calls[0] == 2:
                raise sqlite3.OperationalError("Injected failed write")
            original(*args)

        with patch.object(self.store, "recompute_pin", side_effect=fail):
            with self.assertRaises(sqlite3.OperationalError):
                self.tracker.queue_tree("folder")
        for table in ("files", "jobs", "pin_sources", "folder_intents"):
            self.assertEqual(self.store.connection.execute("SELECT count(*) FROM " + table).fetchone()[0], 0)

    def test_folder_badge_requires_fresh_manifest_all_members_and_read_path(self):
        self.tracker.queue_tree("folder")
        self.tracker.resume()
        identity = self.folder_identity()
        self.assertEqual(self.engine().status(identity).state, State.KEPT_OFFLINE)
        self.assertEqual(StatusEngine(self.config, self.store).status(identity).state, State.UNKNOWN)
        self.store.connection.execute("UPDATE folder_intents SET scanned_at=0")
        self.assertEqual(self.engine().status(identity).state, State.UNKNOWN)
        self.tracker.queue_tree("folder")
        self.store.observe(
            replace(self.store.remote(self.identity), revision="different", observed_at=time.time())
        )
        self.assertEqual(self.engine().status(identity).state, State.UNKNOWN)

    def test_empty_oversize_and_escaped_manifests_do_not_claim_offline(self):
        with self.assertRaises(IntegrityError):
            self.tracker.queue_tree("folder", limit=2)
        self.backend.records = {"folder": self.backend.records["folder"]}
        self.tracker.queue_tree("folder")
        self.assertEqual(self.engine().status(self.folder_identity()).state, State.UNKNOWN)
        self.backend.records["file"] = ("outside.txt", b"x", "v1", False)
        original = self.backend.tree
        self.backend.tree = lambda *args, **kwargs: [
            self.backend.value(item) for item in self.backend.records
        ]
        with self.assertRaises(ScopeError):
            self.tracker.queue_tree("folder")
        self.backend.tree = original

    def test_schema_two_migration_preserves_existing_direct_pin_and_bytes(self):
        old = self.keep()
        self.store.connection.executescript("""
DROP TABLE snapshots; DROP TABLE jobs; DROP TABLE changes; DROP TABLE issues; DROP TABLE folder_intents; DROP TABLE pin_sources;
PRAGMA user_version=2;
""")
        self.reopen()
        self.tracker = ChangeTracker(self.controller)
        self.assertEqual(self.store.connection.execute("PRAGMA user_version").fetchone()[0], 4)
        self.assertEqual(self.store.copy(self.identity), old)
        self.tracker.queue_tree("folder")
        self.tracker.unpin_tree("folder")
        self.assertTrue(self.store.row(self.identity)["pinned"])


class GraphChangeTests(unittest.TestCase):
    setUp = graph_fixtures.GraphTests.setUp
    save = graph_fixtures.GraphTests.save

    def by_id(self):
        original = self.backend.graph
        self.backend.graph = lambda route: self.file if route.endswith("/items/item-id") else original(route)

    def test_stable_id_lookup_observes_new_path_and_rejects_other_identity(self):
        self.by_id()
        self.file["name"] = "renamed.txt"
        identity = FileIdentity("test-drive", "item-id")
        self.assertEqual(self.backend.describe_identity(identity).remote.relative_path, "renamed.txt")
        self.file["id"] = "different"
        with self.assertRaises(ScopeError):
            self.backend.describe_identity(identity)

    def test_missing_id_rechecks_account_and_root_while_http_errors_remain_distinct(self):
        original = self.backend.graph
        self.backend.graph = lambda route: (
            (_ for _ in ()).throw(RemoteMissing("Absent"))
            if route.endswith("/items/item-id")
            else original(route)
        )
        with self.assertRaises(RemoteMissing):
            self.backend.describe_identity(FileIdentity("test-drive", "item-id"))
        self.assertEqual(sum(route.startswith("me/drive?") for route in self.calls), 2)
        for status, expected in (
            (404, RemoteMissing),
            (403, StorageError),
            (429, StorageError),
            (503, StorageError),
        ):
            with patch.object(
                self.backend.opener,
                "open",
                side_effect=urllib.error.HTTPError("private", status, "", {}, None),
            ):
                with self.assertRaises(expected):
                    type(self.backend).graph(self.backend, "drives/test-drive/items/item-id")

    def test_missing_allowed_root_is_never_classified_as_missing_file(self):
        original = self.backend.graph
        self.backend.graph = lambda route: (
            (_ for _ in ()).throw(RemoteMissing("Root unavailable"))
            if route.endswith("/items/allowed-root")
            else original(route)
        )
        with self.assertRaises(StorageError) as caught:
            self.backend.describe_identity(FileIdentity("test-drive", "item-id"))
        self.assertNotIsInstance(caught.exception, RemoteMissing)

    def setup_tree(self, link=None):
        original = self.backend.graph
        folder = {
            "id": "subfolder",
            "name": "folder",
            "folder": {},
            "eTag": "d1",
            "parentReference": {"driveId": "test-drive", "id": "allowed-root"},
        }
        self.file["parentReference"]["id"] = "subfolder"

        def graph(route):
            if ":/folder" in route or route.endswith("/items/subfolder"):
                return folder
            if "/items/subfolder/children" in route:
                page = {"value": [self.file] if "$top" in route else []}
                if link and "$top" in route:
                    page["@odata.nextLink"] = link
                return page
            return original(route)

        self.backend.graph = graph

    def test_bounded_tree_follows_only_same_endpoint_continuations(self):
        self.setup_tree(
            "https://graph.microsoft.com/v1.0/drives/test-drive/items/subfolder/children?$skiptoken=safe"
        )
        values = self.backend.tree("folder")
        self.assertEqual([v.remote.relative_path for v in values], ["folder", "folder/document.txt"])
        with self.assertRaises(StorageError):
            self.backend.tree("folder", limit=1)
        with self.assertRaises(Cancelled):
            self.backend.tree("folder", cancelled=lambda: True)

    def test_malicious_continuation_links_never_receive_credentials(self):
        for link in (
            "https://evil.invalid/v1.0/drives/test-drive/items/subfolder/children?x=1",
            "https://graph.microsoft.com/v1.0/drives/other/items/subfolder/children?x=1",
            "https://graph.microsoft.com/v1.0/drives/test-drive/items/subfolder/children?x=1#fragment",
            "https://user@graph.microsoft.com/v1.0/drives/test-drive/items/subfolder/children?x=1",
        ):
            self.setUp()
            self.setup_tree(link)
            with self.assertRaises(ScopeError):
                self.backend.tree("folder")

    def test_duplicate_children_and_shared_items_abort_entire_manifest(self):
        self.setup_tree()
        original = self.backend.graph
        self.backend.graph = lambda route: (
            {"value": [self.file, self.file]} if "/children" in route else original(route)
        )
        with self.assertRaises(ScopeError):
            self.backend.tree("folder")
        self.file["remoteItem"] = {}
        with self.assertRaises(ScopeError):
            self.backend.tree("folder")
