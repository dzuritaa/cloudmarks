"""M6 archive transactions, read-only diagnostics and large-file proof boundaries."""

import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path
from unittest.mock import patch

from cloudmarks.diagnostics import doctor
from cloudmarks.helper import OfflineStatusService
from cloudmarks.model import IntegrityError, ScopeError, State, StorageError
from cloudmarks.onedrive import Cancelled
from cloudmarks.paths import SecureRoot
from cloudmarks.recovery import ConflictRecovery, measure
from cloudmarks.state import ReadStore
from cloudmarks.verification import INLINE_LIMIT, status_engine, verify
from tests.test_offline import OfflineCase
from tests.test_tracking import TrackingCase


class ArchiveTests(TrackingCase):
    def conflict(self, *, renamed=False):
        old = self.keep()
        os.chmod(self.visible(), 0o600)
        self.visible().write_bytes(b"User's retained local version.\n")
        self.change(path="folder/renamed.txt" if renamed else None)
        self.tracker.refresh(self.path)
        self.tracker.resume()
        self.assertEqual(self.state(), "blocked")
        return old

    def test_archive_retry_preserves_three_versions_and_downloads_at_renamed_path(self):
        old = self.conflict(renamed=True)
        local, original, candidate = (
            self.visible().read_bytes(),
            (self.disk / old.object_path).read_bytes(),
            self.candidate_bytes(),
        )
        receipt = ConflictRecovery(self.controller).resolve(self.path, retry_remote=True)
        self.assertEqual(receipt["outcome"], "retry_queued")
        self.assertFalse(self.visible().exists())
        self.assertFalse(self.tracker.conflicts())
        self.assertEqual(self.state(), "pending")
        for name, content in (("local", local), ("original", original), ("remote_candidate", candidate)):
            self.assertEqual((self.disk / receipt["sources"][name]["path"]).read_bytes(), content)
        self.tracker.resume()
        self.assertEqual(self.state(), "done")
        self.assertEqual((self.disk / "local/folder/renamed.txt").read_bytes(), candidate)
        self.assertEqual(self.store.row(self.identity)["dirty"], 0)
        self.assertEqual(len(ConflictRecovery(self.controller).archives()), 1)
        self.reopen()
        self.assertEqual(len(ConflictRecovery(self.controller).archives()), 1)

    def test_remote_change_or_path_collision_during_review_retains_active_entry(self):
        self.conflict()
        before = self.visible().read_bytes()
        first = self.backend.value("file")
        self.change(revision="v3")
        second = self.backend.value("file")
        with (
            patch.object(self.backend, "describe_identity", side_effect=[first, second]),
            self.assertRaises(ScopeError),
        ):
            ConflictRecovery(self.controller).resolve(self.path, retry_remote=True)
        self.assertEqual(self.visible().read_bytes(), before)
        with SecureRoot(self.config.storage_root, self.config.volume_id) as root:
            with (
                patch.object(root, "current_fingerprint", side_effect=FileNotFoundError),
                self.assertRaises(FileNotFoundError),
            ):
                measure(root, "local/" + self.path)
        # A conflicting catalog identity must roll the filesystem move back too.
        from dataclasses import replace

        self.change(path="folder/occupied.txt", revision="v4")
        occupied = replace(
            self.backend.value("file").remote,
            identity=type(self.identity)(self.identity.drive_id, "occupant"),
        )
        self.store.observe(occupied)
        with self.assertRaises(sqlite3.IntegrityError):
            ConflictRecovery(self.controller).resolve(self.path, retry_remote=True)
        self.assertEqual(self.visible().read_bytes(), before)
        self.assertFalse(ConflictRecovery(self.controller).archives())
        self.assertTrue(self.tracker.conflicts())

    def test_archive_only_retires_identity_and_frees_path_without_cloud_calls(self):
        self.conflict()
        self.backend.fail = True
        receipt = ConflictRecovery(self.controller).resolve(self.path)
        self.assertEqual(receipt["outcome"], "retired")
        self.assertIsNone(self.store.row(self.identity))
        self.assertFalse(self.visible().exists())
        self.backend.fail = False
        self.backend.records["replacement"] = self.backend.records.pop("file")
        self.tracker.queue(self.path)
        self.tracker.resume()
        self.assertTrue(self.visible().exists())
        self.assertTrue((self.disk / receipt["sources"]["local"]["path"]).exists())

    def test_missing_cloud_can_be_archived_but_retry_refuses(self):
        self.keep()
        self.backend.missing = True
        self.tracker.refresh(self.path)
        before = self.visible().read_bytes()
        with self.assertRaises(StorageError):
            # RemoteMissing inherits StorageError.
            ConflictRecovery(self.controller).resolve(self.path, retry_remote=True)
        self.assertEqual(self.visible().read_bytes(), before)
        receipt = ConflictRecovery(self.controller).resolve(self.path)
        self.assertTrue(receipt["remote"]["deleted"])
        self.assertEqual((self.disk / receipt["sources"]["local"]["path"]).read_bytes(), before)

    def test_export_is_private_complete_and_never_overwrites(self):
        self.conflict()
        receipt = ConflictRecovery(self.controller).resolve(self.path)
        with ReadStore(self.config) as reader:
            self.controller.store = reader
            recovery = ConflictRecovery(self.controller)
            target = self.root / "export"
            before = self.config.state_path.read_bytes()
            recovery.export(receipt["archive_id"], target)
            self.assertEqual(self.config.state_path.read_bytes(), before)
            manifest = json.loads((target / "manifest.json").read_text())
            self.assertEqual(manifest, receipt)
            self.assertEqual(target.stat().st_mode & 0o777, 0o700)
            for name, source in receipt["sources"].items():
                exported = target / (name + ".bin")
                self.assertEqual(exported.stat().st_mode & 0o777, 0o600)
                self.assertEqual(hashlib.sha256(exported.read_bytes()).hexdigest(), source["sha256"])
            with self.assertRaises(StorageError):
                recovery.export(receipt["archive_id"], target)
            for forbidden in (
                self.disk / "export",
                self.private / "export",
                Path(self.settings["visible_root"]) / "export",
            ):
                with self.assertRaises(ScopeError):
                    recovery.export(receipt["archive_id"], forbidden)
            link = self.root / "link"
            link.symlink_to(self.disk, target_is_directory=True)
            with self.assertRaises(OSError):
                recovery.export(receipt["archive_id"], link / "export")
        self.controller.store = self.store

    def test_export_detects_changed_archived_source_and_keeps_incomplete_directory(self):
        self.conflict()
        recovery = ConflictRecovery(self.controller)
        receipt = recovery.resolve(self.path)
        source = self.disk / receipt["sources"]["local"]["path"]
        source.write_bytes(b"Unexpected external mutation")
        destination = self.root / "incomplete"
        with self.assertRaises(IntegrityError):
            recovery.export(receipt["archive_id"], destination)
        self.assertTrue(destination.exists())
        self.assertFalse((destination / "manifest.json").exists())
        self.assertTrue(source.exists())

    def test_missing_visible_and_corrupt_object_are_reported_truthfully(self):
        old = self.conflict()
        self.visible().unlink()
        obj = self.disk / old.object_path
        os.chmod(obj, 0o600)
        obj.write_bytes(b"Damaged immutable original")
        receipt = ConflictRecovery(self.controller).resolve(self.path)
        self.assertTrue(receipt["sources"]["local"]["missing"])
        self.assertEqual(
            receipt["sources"]["original"]["sha256"], hashlib.sha256(obj.read_bytes()).hexdigest()
        )
        self.assertNotEqual(receipt["sources"]["original"]["sha256"], receipt["entry"]["copy"]["sha256"])

    def test_interruption_rolls_back_modified_bytes_at_each_uncommitted_boundary(self):
        for boundary in ("archive-intent", "archive-visible"):
            with self.subTest(boundary=boundary):
                if not self.store.copy(self.identity):
                    self.keep()
                if not self.tracker.conflicts():
                    self.conflict()
                before = self.visible().read_bytes()

                def stop(phase, boundary=boundary):
                    if phase == boundary:
                        raise RuntimeError("Injected failure")

                self.controller.hook = stop
                with self.assertRaises(RuntimeError):
                    ConflictRecovery(self.controller).resolve(self.path)
                self.assertEqual(self.visible().read_bytes(), before)
                self.assertFalse(ConflictRecovery(self.controller).archives())
                self.assertTrue(self.tracker.conflicts())
                self.assertEqual(
                    self.store.connection.execute("SELECT count(*) FROM publications").fetchone()[0], 0
                )
                self.controller.recover()  # Idempotent.
                self.controller.hook = lambda phase: None

    def test_abrupt_death_leaves_recoverable_journal_and_commit_is_final(self):
        self.conflict()
        before = self.visible().read_bytes()
        pid = os.fork()
        if pid == 0:
            self.controller.hook = lambda phase: os._exit(71) if phase == "archive-visible" else None
            ConflictRecovery(self.controller).resolve(self.path)
            os._exit(72)
        _, status = os.waitpid(pid, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), 71)
        self.reopen()
        self.controller.recover()
        self.assertEqual(self.visible().read_bytes(), before)
        # Force death after the transaction: recovery must not restore retired content.
        pid = os.fork()
        if pid == 0:
            self.controller.hook = lambda phase: os._exit(73) if phase == "archive-committed" else None
            ConflictRecovery(self.controller).resolve(self.path)
            os._exit(74)
        _, status = os.waitpid(pid, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), 73)
        self.reopen()
        self.controller.recover()
        self.assertFalse(self.visible().exists())
        self.assertEqual(len(ConflictRecovery(self.controller).archives()), 1)

    def test_sqlite_failure_rolls_back_archive_and_never_discards_bytes(self):
        self.conflict()
        before = self.visible().read_bytes()
        self.store.connection.execute(
            "CREATE TRIGGER fail_archive BEFORE INSERT ON archives BEGIN SELECT RAISE(ABORT,'injected full disk'); END"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            ConflictRecovery(self.controller).resolve(self.path)
        self.assertEqual(self.visible().read_bytes(), before)
        self.assertTrue(self.store.copy(self.identity))
        self.assertFalse(ConflictRecovery(self.controller).archives())

    def test_no_overwrite_on_rollback_and_cancel_before_intent(self):
        self.conflict()
        self.controller.cancelled = lambda: True
        with self.assertRaises(Cancelled):
            ConflictRecovery(self.controller).resolve(self.path)
        self.controller.cancelled = lambda: False

        def hook(phase):
            if phase == "archive-visible":
                self.visible().write_bytes(b"Unexpected new pathname occupant")
                raise RuntimeError("Interrupted")

        self.controller.hook = hook
        with self.assertRaises(FileExistsError):
            ConflictRecovery(self.controller).resolve(self.path)
        self.assertEqual(self.visible().read_bytes(), b"Unexpected new pathname occupant")
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM publications").fetchone()[0], 1)
        self.assertTrue(list((self.disk / "recovery").glob("*.local")))

    def test_symlink_and_mounted_view_refuse_without_archiving(self):
        self.conflict()
        original = self.visible().read_bytes()
        outside = self.root / "outside"
        outside.write_bytes(original)
        self.visible().unlink()
        self.visible().symlink_to(outside)
        with self.assertRaises(OSError):
            ConflictRecovery(self.controller).resolve(self.path)
        self.assertEqual(outside.read_bytes(), original)
        self.visible().unlink()
        self.visible().write_bytes(original)
        self.controller.unmounted = lambda settings: (_ for _ in ()).throw(StorageError("Mounted"))
        with self.assertRaises(StorageError):
            ConflictRecovery(self.controller).resolve(self.path)
        self.assertFalse(ConflictRecovery(self.controller).archives())


class VerificationTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.settings["budget_bytes"] = 128 * 1024 * 1024
        self.backend.content = b"M6 large fixture\n" * (INLINE_LIMIT // 17 + 2)
        # QuickXor's Python implementation is intentionally slow; the ordinary
        # backend fixture can use SHA-256 here without changing production.
        describe = self.backend.describe

        def sha_observation(path):
            # Build metadata without running the fixture's QuickXor loop.
            from cloudmarks.model import RemoteFile
            from cloudmarks.onedrive import Observation

            return Observation(
                RemoteFile(
                    self.identity,
                    self.config.allowed_root_id,
                    self.path,
                    self.backend.revision,
                    len(self.backend.content),
                    time.time(),
                ),
                "sha256Hash",
                hashlib.sha256(self.backend.content).digest(),
            )

        self.backend.describe = sha_observation
        self.backend.chunks = lambda observation, **kwargs: (
            self.backend.content[offset : offset + 1024 * 1024]
            for offset in range(0, len(self.backend.content), 1024 * 1024)
        )
        self.assertTrue(callable(describe))
        self.keep()
        self.attestation = patch("cloudmarks.verification.attest_view", return_value=True)
        self.attestation.start()
        self.addCleanup(self.attestation.stop)

    def engine(self, *, clock=time.time):
        return status_engine(self.config, self.store, self.settings, clock=clock)

    def test_explicit_verify_enables_bounded_read_only_helper_without_hashing_or_transport(self):
        self.assertEqual(self.engine().status(self.identity).state, State.UNKNOWN)
        verify(self.controller, self.path)
        before = self.config.state_path.read_bytes()
        with (
            ReadStore(self.config) as reader,
            patch.object(SecureRoot, "verify", side_effect=AssertionError("Helper rehashed content")),
            patch("cloudmarks.onedrive.OneDrive", side_effect=AssertionError("Constructed transport")),
        ):
            service = OfflineStatusService(
                {"visible_root": self.settings["visible_root"]}, reader, self.settings
            )
            for _ in range(5):
                self.assertEqual(service.lookup(self.path)["state"], "kept_offline")
        self.assertEqual(self.config.state_path.read_bytes(), before)

    def test_proof_expiry_and_remote_expiry_fail_closed(self):
        from dataclasses import replace

        now = time.time()
        self.store.observe(replace(self.store.remote(self.identity), observed_at=now))
        verify(self.controller, self.path, clock=lambda: now)
        self.assertEqual(self.engine(clock=lambda: now + 299).status(self.identity).state, State.KEPT_OFFLINE)
        self.assertEqual(self.engine(clock=lambda: now + 300).status(self.identity).state, State.UNKNOWN)
        self.assertEqual(self.engine(clock=lambda: now - 1).status(self.identity).state, State.UNKNOWN)
        verify(self.controller, self.path, clock=lambda: now + 100)
        expired = self.engine(clock=lambda: now + 301).status(self.identity)
        self.assertEqual(expired.state, State.UNKNOWN)
        self.assertIn("Remote observation", expired.reason)

    def test_same_size_local_tamper_with_restored_mtime_is_detected(self):
        verify(self.controller, self.path)
        original = self.visible().stat()
        os.chmod(self.visible(), 0o600)
        with self.visible().open("r+b") as stream:
            stream.write(b"X")
        os.utime(self.visible(), ns=(original.st_atime_ns, original.st_mtime_ns))
        self.assertEqual(self.engine().status(self.identity).state, State.ERROR)
        with self.assertRaises(IntegrityError):
            verify(self.controller, self.path)

    def test_object_replacement_and_missing_mount_invalidate_evidence(self):
        verify(self.controller, self.path)
        with patch("cloudmarks.verification.attest_view", return_value=False):
            self.assertEqual(self.engine().status(self.identity).state, State.UNKNOWN)
            with self.assertRaises(ScopeError):
                verify(self.controller, self.path)
        # A remount with independently valid attestation can reuse unchanged proof.
        self.assertEqual(self.engine().status(self.identity).state, State.KEPT_OFFLINE)
        obj = self.disk / self.store.copy(self.identity).object_path
        data = obj.read_bytes()
        obj.unlink()
        obj.write_bytes(data)
        self.assertEqual(self.engine().status(self.identity).state, State.ERROR)

    def test_large_file_lease_does_not_outlive_proof_and_metadata_changes_invalidate_it(self):
        now = time.time()
        verify(self.controller, self.path, clock=lambda: now)
        service = OfflineStatusService(
            {"visible_root": self.settings["visible_root"]}, self.store, self.settings
        )
        # Independently keep remote evidence fresh long enough to test proof expiry.
        from dataclasses import replace

        self.store.observe(replace(self.store.remote(self.identity), observed_at=now + 1))
        with patch("time.time", return_value=now + 299.75):
            service.engine = self.engine(clock=lambda: now + 299.75)
            result = service.lookup(self.path)
            self.assertEqual(result["state"], "kept_offline")
            self.assertLessEqual(result["valid_for_ms"], 250)
        self.store.observe(
            replace(self.store.remote(self.identity), revision="unseen-new-revision", observed_at=now + 2)
        )
        self.assertEqual(self.engine(clock=lambda: now + 3).status(self.identity).state, State.UNKNOWN)

    def test_dirty_stale_or_cancelled_verification_never_creates_proof(self):
        self.store.set_dirty(self.identity, True)
        with self.assertRaises(ScopeError):
            verify(self.controller, self.path)
        self.store.set_dirty(self.identity, False)
        with self.assertRaises(ScopeError):
            verify(self.controller, self.path, clock=lambda: time.time() + 301)
        self.controller.cancelled = lambda: True
        with self.assertRaises(Cancelled):
            verify(self.controller, self.path)
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM verifications").fetchone()[0], 0)

    def test_mutation_during_full_read_fails(self):
        actual = measure

        def race(root, path, cancelled, **kwargs):
            result = actual(root, path, cancelled, **kwargs)
            if path.startswith("local/"):
                os.chmod(self.visible(), 0o600)
                with self.visible().open("r+b") as stream:
                    stream.write(b"Z")
            return result

        with patch("cloudmarks.verification.measure", side_effect=race), self.assertRaises(IntegrityError):
            verify(self.controller, self.path)


class DiagnosticsTests(OfflineCase):
    def test_read_only_cli_reports_never_construct_transport_or_change_state(self):
        import contextlib
        import io
        from types import SimpleNamespace

        from cloudmarks.offline_cli import run

        self.keep()
        before = self.config.state_path.read_bytes()
        with (
            patch("cloudmarks.offline_cli.load_offline", return_value=(self.config, self.settings)),
            patch("cloudmarks.offline_cli.OneDrive", side_effect=AssertionError("Transport constructed")),
        ):
            for action in ("archives", "doctor", "status"):
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    run(SimpleNamespace(config="unused", action=action, path=self.path))
                json.loads(output.getvalue())
        self.assertEqual(self.config.state_path.read_bytes(), before)

    def test_explicit_verify_cli_uses_no_transport_and_reports_progress(self):
        import contextlib
        import io
        from types import SimpleNamespace

        from cloudmarks.offline_cli import run

        self.keep()
        self.store.close()
        with (
            patch("cloudmarks.offline_cli.load_offline", return_value=(self.config, self.settings)),
            patch("cloudmarks.offline_cli.OneDrive", side_effect=AssertionError("Transport constructed")),
            patch("cloudmarks.verification.attest_view", return_value=True),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            run(SimpleNamespace(config="unused", action="verify", path=self.path))
        self.reopen()
        values = [json.loads(line) for line in output.getvalue().splitlines()]
        phases = {value["phase"] for value in values if value.get("completed") == len(self.backend.content)}
        self.assertEqual(phases, {"object", "local"})
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM verifications").fetchone()[0], 1)

    def test_health_is_sanitized_read_only_and_does_not_construct_network(self):
        self.keep()
        before = self.config.state_path.read_bytes()
        with (
            ReadStore(self.config) as reader,
            patch("cloudmarks.diagnostics.mounted_view", return_value=None),
            patch("cloudmarks.onedrive.OneDrive", side_effect=AssertionError("Network")),
        ):
            self.controller.store = reader
            report = doctor(self.controller)
        self.controller.store = self.store
        self.assertTrue(report["ready_for_trial"])
        self.assertEqual(report["counts"]["copies"], 1)
        for private in (str(self.disk), self.config.volume_id, self.identity.item_id, self.identity.drive_id):
            self.assertNotIn(private, json.dumps(report))
        self.assertEqual(self.config.state_path.read_bytes(), before)

    def test_schema_four_migration_failure_rolls_back_both_tables_and_version(self):
        self.keep()
        self.store.connection.executescript(
            "DROP TABLE archives; DROP TABLE verifications; PRAGMA user_version=3;"
        )
        self.store.close()
        from cloudmarks.state import StateStore

        connect = sqlite3.connect

        def denied(*args, **kwargs):
            connection = connect(*args, **kwargs)
            connection.set_authorizer(
                lambda action, name, *unused: (
                    sqlite3.SQLITE_DENY
                    if action == sqlite3.SQLITE_CREATE_TABLE and name == "verifications"
                    else sqlite3.SQLITE_OK
                )
            )
            return connection

        with (
            patch("cloudmarks.state.sqlite3.connect", side_effect=denied),
            self.assertRaises(sqlite3.DatabaseError),
        ):
            StateStore(self.config)
        with ReadStore(self.config) as reader:
            self.assertEqual(reader.connection.execute("PRAGMA user_version").fetchone()[0], 3)
            self.assertFalse(
                reader.connection.execute("SELECT name FROM sqlite_master WHERE name='archives'").fetchone()
            )
        self.reopen()
        self.assertEqual(self.store.connection.execute("PRAGMA user_version").fetchone()[0], 4)

    def test_legacy_schema_read_only_never_migrates_then_writer_preserves_bytes(self):
        old = self.keep()
        self.store.connection.executescript(
            "DROP TABLE archives; DROP TABLE verifications; PRAGMA user_version=3;"
        )
        self.store.close()
        before = self.config.state_path.read_bytes()
        with ReadStore(self.config) as reader:
            self.assertEqual(reader.connection.execute("PRAGMA user_version").fetchone()[0], 3)
        self.assertEqual(self.config.state_path.read_bytes(), before)
        self.reopen()
        self.assertEqual(self.store.connection.execute("PRAGMA user_version").fetchone()[0], 4)
        self.assertEqual(self.store.copy(self.identity), old)
        self.assertEqual(self.visible().read_bytes(), self.backend.content)
