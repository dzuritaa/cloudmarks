"""State migrations, the release journal and the read-only integrity check."""

import json
import unittest

from cloudmarks.engine import UNAVAILABLE_REASONS, StatusEngine
from cloudmarks.model import IntegrityError, State, StorageError
from cloudmarks.state import SCHEMA_VERSION, ReadStore
from tests.test_offline import OfflineCase
from tests.test_tracking import TrackingCase

# Tables each schema version added; a downgrade drops them in reverse.
ADDED = {
    6: ["releases"],
    5: [],
    4: ["archives", "verifications"],
    3: ["jobs", "changes", "issues", "folder_intents", "pin_sources", "snapshots"],
    2: ["local_entries", "publications"],
}
INDEXES = {5: ["local_entries_path_key", "pin_sources_owner"]}


class MigrationTests(OfflineCase):
    def downgrade(self, version):
        statements = []
        for step in range(SCHEMA_VERSION, version, -1):
            statements += [f"DROP INDEX IF EXISTS {name};" for name in INDEXES.get(step, [])]
            statements += [f"DROP TABLE {name};" for name in ADDED[step]]
        self.store.connection.executescript(
            "PRAGMA foreign_keys=OFF;" + "".join(statements) + f"PRAGMA user_version={version};"
        )

    def tables(self):
        return {row[0] for row in self.store.connection.execute("SELECT name FROM sqlite_master")}

    def upgrades_from(self, version):
        copy = self.keep()
        expected = self.tables()
        self.downgrade(version)
        self.reopen()
        self.assertEqual(self.store.schema_version(), SCHEMA_VERSION)
        self.assertEqual(self.tables(), expected)
        self.assertEqual(self.store.copy(self.identity), copy)
        self.assertEqual(self.store.check_integrity(), [])

    def test_schema_5_upgrades(self):
        self.upgrades_from(5)

    def test_schema_4_upgrades(self):
        self.upgrades_from(4)

    def test_schema_3_upgrades(self):
        self.upgrades_from(3)

    def test_schema_2_upgrades(self):
        self.upgrades_from(2)

    def test_read_only_store_accepts_every_supported_version_without_migrating(self):
        self.keep()
        self.downgrade(5)
        self.store.close()
        with ReadStore(self.config) as reader:
            self.assertEqual(reader.schema_version(), 5)
            self.assertEqual(reader.pending_releases(), [])
            self.assertEqual(reader.check_integrity(), [])


class ReleaseJournalTests(TrackingCase):
    def test_unfinished_release_is_restored_when_a_writer_opens(self):
        self.tracker.queue_tree("folder")
        owner = "folder:folder"
        pins = self.store.pin_sources_owned(owner)
        folder = self.tracker.identity_at("folder")
        self.assertTrue(pins)
        self.store.begin_release(folder, pins, 1)
        self.tracker.unpin_tree("folder")
        self.assertEqual(self.store.check_integrity(), ["unfinished_release"])
        self.reopen()  # The previous writer "died" without finish_release.
        self.assertEqual(len(self.store.pin_sources_owned(owner)), len(pins))
        self.assertTrue(self.store.row(self.identity)["pinned"])
        self.assertEqual(self.store.folder_intent(folder)["pinned"], 1)
        self.assertEqual(self.store.pending_releases(), [])
        self.assertEqual(self.store.check_integrity(), [])

    def test_restore_is_idempotent_and_skips_items_without_observations(self):
        self.tracker.queue(self.path)
        pins = self.store.direct_pins(self.identity)
        self.store.begin_release(self.identity, [*pins, ("test-drive", "gone", "direct")], None)
        self.assertEqual(self.store.restore_releases(), 1)
        self.assertEqual(self.store.restore_releases(), 0)
        self.assertEqual(self.store.direct_pins(self.identity), pins)

    def test_a_second_release_of_the_same_selection_is_refused(self):
        self.tracker.queue(self.path)
        self.store.begin_release(self.identity, [], None)
        from cloudmarks.model import ScopeError

        with self.assertRaises(ScopeError):
            self.store.begin_release(self.identity, [], None)

    def test_unknown_release_format_is_preserved(self):
        self.tracker.queue(self.path)
        self.store.connection.execute(
            "INSERT INTO releases VALUES (?,?,?)",
            (*self.store.key(self.identity), json.dumps({"version": 9})),
        )
        with self.assertRaises(StorageError):
            self.store.restore_releases()
        self.assertEqual(len(self.store.pending_releases()), 1)


class JobStateTests(TrackingCase):
    def test_job_transitions_are_validated_and_count_attempts(self):
        from cloudmarks.model import ConfigurationError

        self.tracker.queue(self.path)
        self.store.set_job(self.identity, "running")
        self.store.set_job(self.identity, "error", "x" * 400)
        job = self.tracker.jobs()[0]
        self.assertEqual((job["status"], job["attempts"], len(job["error"])), ("error", 1, 256))
        with self.assertRaises(ConfigurationError):
            self.store.set_job(self.identity, "paused")

class IntegrityTests(OfflineCase):
    def test_clean_store_has_no_problems(self):
        self.keep()
        self.assertEqual(self.store.check_integrity(), [])

    def test_broken_invariants_are_named(self):
        self.keep()
        connection = self.store.connection
        connection.execute("UPDATE files SET pinned=0")
        self.assertEqual(self.store.check_integrity(), ["pin_flag_mismatch"])
        connection.execute("UPDATE files SET pinned=1")
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("DELETE FROM local_entries")
        self.assertEqual(self.store.check_integrity(), ["copy_without_publication"])
        connection.execute("INSERT INTO publications VALUES ('test-drive','x','{}')")
        self.assertIn("interrupted_operation", self.store.check_integrity())

    def test_doctor_reports_state_problems_and_is_not_ready(self):
        from cloudmarks.diagnostics import doctor

        self.keep()
        self.store.connection.execute("UPDATE files SET pinned=0")
        report = doctor(self.controller)
        self.assertEqual(report["state_problems"], ["pin_flag_mismatch"])
        self.assertFalse(report["ready_for_trial"])


class FailureReasonTests(TrackingCase):
    def test_status_failures_keep_their_cause(self):
        import sqlite3

        categories = []
        engine = StatusEngine(self.config, self.store, report=categories.append)
        for error, category in (
            (sqlite3.OperationalError("database is locked"), "database"),
            (StorageError("disk gone"), "storage"),
        ):
            self.store.remote = lambda identity, error=error: (_ for _ in ()).throw(error)
            status = engine.status(self.identity)
            self.assertEqual((status.state, status.reason), (State.UNKNOWN, UNAVAILABLE_REASONS[category]))
        del self.store.remote
        self.assertEqual(categories, ["database", "storage"])

    def test_resume_records_a_fixed_message_per_cause(self):
        from unittest.mock import patch

        from cloudmarks.tracking import RESUME_REASONS

        self.tracker.queue(self.path)
        with patch.object(self.controller, "keep", side_effect=IntegrityError("private /home/x detail")):
            self.tracker.resume()
        job = self.tracker.jobs()[0]
        self.assertEqual(job["status"], "error")
        self.assertEqual(job["error"], RESUME_REASONS["integrity"])
        self.assertNotIn("/home", job["error"])


if __name__ == "__main__":
    unittest.main()
