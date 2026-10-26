import errno
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from cloudmarks.config import Config, load_config
from cloudmarks.engine import StatusEngine
from cloudmarks.fixture import FixtureBackend, FixtureController
from cloudmarks.model import (
    ConfigurationError,
    FileIdentity,
    IntegrityError,
    LocalCopy,
    ScopeError,
    State,
    StorageError,
)
from cloudmarks.paths import MARKER, relative_parts
from cloudmarks.state import StateStore


class CoreCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(
            prefix="cloudmarks-test-", dir=os.environ.get("CLOUDMARKS_TEST_TMPDIR")
        )
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.disk = self.root / "disk"
        self.private = self.root / "state"
        self.disk.mkdir(mode=0o700)
        self.private.mkdir(mode=0o700)
        (self.disk / MARKER).write_text("fixture-volume\n")
        self.config = Config(
            "test-drive",
            "allowed-root",
            ("personal-drive",),
            self.disk,
            "fixture-volume",
            self.private / "state.sqlite3",
            max_age_seconds=60,
        )
        self.now = [1000.0]
        self.store = StateStore(self.config)
        self.addCleanup(lambda: self.store.close())
        self.backend = FixtureBackend(self.config, clock=lambda: self.now[0])
        self.identity = FileIdentity("test-drive", "stable-item")
        self.content = b"Verified fixture content with more than one transport chunk.\n"
        self.remote = self.backend.put(self.identity, "folder/disposable.txt", self.content)
        self.store.observe(self.remote)
        self.controller = FixtureController(self.config, self.store, self.backend)
        self.engine = StatusEngine(
            self.config, self.store, read_path=self.backend.prove_read_path, clock=lambda: self.now[0]
        )

    def complete(self, pinned=False):
        copy = self.controller.download(self.identity)
        self.store.set_pin(self.identity, pinned)
        return copy

    def new_version(self, content=b"New remote revision, different complete contents.\n"):
        self.now[0] += 1
        return self.backend.put(self.identity, self.remote.relative_path, content, "fixture-v2")

    def status(self):
        return self.engine.status(self.identity)

    def reopen(self):
        self.store.close()
        self.store = StateStore(self.config)
        self.controller = FixtureController(self.config, self.store, self.backend)
        self.engine = StatusEngine(
            self.config, self.store, read_path=self.backend.prove_read_path, clock=lambda: self.now[0]
        )


class StatusTests(CoreCase):
    def test_known_remote_without_local_content_is_cloud_only(self):
        self.assertEqual(self.status().state, State.CLOUD_ONLY)

    def test_unknown_identity_does_not_guess_cloud_only(self):
        self.assertEqual(self.engine.status(FileIdentity("test-drive", "unknown")).state, State.UNKNOWN)

    def test_complete_verified_file_and_pin_intent_are_separate(self):
        self.complete()
        self.assertEqual(self.status().state, State.SAVED_LOCALLY)
        self.store.set_pin(self.identity, True)
        self.assertEqual(self.status().state, State.KEPT_OFFLINE)
        self.store.set_pin(self.identity, False)
        self.assertEqual(self.status().state, State.SAVED_LOCALLY)

    def test_pin_intent_alone_never_becomes_local(self):
        self.store.set_pin(self.identity, True)
        self.assertEqual(self.status().state, State.CLOUD_ONLY)

    def test_in_progress_is_visible_before_any_success(self):
        states = []
        self.backend.chunk_hook = lambda _: states.append(self.status().state)
        self.complete()
        self.assertTrue(states)
        self.assertEqual(set(states), {State.TRANSFERRING})

    def test_missing_open_path_proof_is_unknown(self):
        self.complete()
        self.backend.read_path_available = False
        self.assertEqual(self.status().state, State.UNKNOWN)

    def test_default_engine_has_no_implicit_read_path_proof(self):
        self.complete()
        self.assertEqual(
            StatusEngine(self.config, self.store, clock=lambda: self.now[0]).status(self.identity).state,
            State.UNKNOWN,
        )

    def test_read_path_provider_failure_is_unknown(self):
        self.complete()
        self.engine.read_path = lambda *_: (_ for _ in ()).throw(RuntimeError("helper unavailable"))
        self.assertEqual(self.status().state, State.UNKNOWN)

    def test_stale_remote_evidence_invalidates_green_but_preserves_bytes(self):
        copy = self.complete(pinned=True)
        self.now[0] += 61
        self.assertEqual(self.status().state, State.UNKNOWN)
        self.assertEqual((self.disk / copy.object_path).read_bytes(), self.content)

    def test_future_observation_and_nonfinite_clock_are_unknown(self):
        self.complete()
        self.engine.clock = lambda: 999.0
        self.assertEqual(self.status().state, State.UNKNOWN)
        self.engine.clock = lambda: float("nan")
        self.assertEqual(self.status().state, State.UNKNOWN)

    def test_new_remote_revision_invalidates_old_complete_copy(self):
        old = self.complete(pinned=True)
        self.store.observe(self.new_version())
        self.assertEqual(self.status().state, State.UNKNOWN)
        self.assertEqual(self.store.copy(self.identity), old)

    def test_remote_deletion_preserves_snapshot(self):
        copy = self.complete()
        self.store.observe(replace(self.remote, deleted=True))
        self.assertEqual(self.status().state, State.UNKNOWN)
        self.assertTrue((self.disk / copy.object_path).exists())

    def test_modified_local_content_is_error_even_at_same_length(self):
        copy = self.complete()
        (self.disk / copy.object_path).write_bytes(b"X" * len(self.content))
        self.assertEqual(self.status().state, State.ERROR)

    def test_truncated_and_missing_objects_never_become_saved(self):
        copy = self.complete()
        path = self.disk / copy.object_path
        path.write_bytes(self.content[:8])
        self.assertEqual(self.status().state, State.ERROR)
        path.unlink()
        self.assertEqual(self.status().state, State.UNKNOWN)

    def test_dirty_flag_is_actionable_and_blocks_download(self):
        copy = self.complete()
        self.store.set_dirty(self.identity, True)
        self.assertEqual(self.status().state, State.ERROR)
        with self.assertRaises(ScopeError):
            self.controller.download(self.identity)
        self.assertEqual(self.store.copy(self.identity), copy)

    def test_empty_file_requires_complete_empty_content_proof(self):
        identity = FileIdentity("test-drive", "empty-item")
        self.backend.put(identity, "empty.txt", b"")
        self.controller.download(identity)
        self.assertEqual(self.engine.status(identity).state, State.SAVED_LOCALLY)

    def test_directory_never_inherits_green_from_its_existence(self):
        folder = replace(self.remote, is_directory=True, size=0, sha256=None)
        self.store.observe(folder)
        self.assertEqual(self.status().state, State.UNKNOWN)
        with self.assertRaises(ScopeError):
            self.store.begin_transfer(self.identity)

    def test_manifest_row_without_live_bytes_cannot_produce_success(self):
        self.store.begin_transfer(self.identity)
        copy = LocalCopy(
            self.identity,
            self.remote.revision,
            "objects/" + "a" * 32 + ".blob",
            self.remote.size,
            self.remote.sha256,
        )
        self.store.record_complete(copy)
        self.engine.read_path = lambda *_: True
        self.assertEqual(self.status().state, State.UNKNOWN)

    def test_closed_or_unavailable_state_is_unknown(self):
        self.store.close()
        self.assertEqual(self.status().state, State.UNKNOWN)

    def test_rename_keeps_identity_but_requires_new_read_path_proof(self):
        old = self.complete()
        renamed = replace(self.remote, relative_path="renamed.txt")
        self.store.observe(renamed)
        self.assertEqual(self.store.copy(self.identity), old)
        self.assertEqual(self.status().state, State.UNKNOWN)

    def test_simultaneous_file_change_during_hash_is_not_green(self):
        copy = self.complete()
        path = self.disk / copy.object_path
        inode = path.stat().st_ino
        original_read = os.read
        changed = []

        def read_then_change(fd, count):
            data = original_read(fd, count)
            if os.fstat(fd).st_ino == inode and data and not changed:
                path.write_bytes(b"X" * len(self.content))
                changed.append(True)
            return data

        with patch("cloudmarks.paths.os.read", side_effect=read_then_change):
            self.assertEqual(self.status().state, State.ERROR)

    def test_expired_evidence_during_read_path_check_is_unknown(self):
        self.complete()

        def proof(*_):
            self.now[0] += 61
            return True

        self.engine.read_path = proof
        self.assertEqual(self.status().state, State.UNKNOWN)

    def test_content_mutation_during_read_path_check_is_not_green(self):
        copy = self.complete()

        def proof(*_):
            (self.disk / copy.object_path).write_bytes(b"X" * len(self.content))
            return True

        self.engine.read_path = proof
        self.assertEqual(self.status().state, State.ERROR)

    def test_disk_disappearance_during_read_path_check_is_unknown(self):
        self.complete()

        def proof(*_):
            (self.disk / MARKER).unlink()
            return True

        self.engine.read_path = proof
        self.assertEqual(self.status().state, State.UNKNOWN)

    def test_new_revision_during_read_path_check_is_unknown(self):
        self.complete()

        def proof(*_):
            self.store.observe(self.new_version())
            return True

        self.engine.read_path = proof
        self.assertEqual(self.status().state, State.UNKNOWN)

    def test_engine_and_store_root_configuration_must_agree(self):
        self.complete()
        engine = StatusEngine(
            replace(self.config, allowed_root_id="other-root"),
            self.store,
            read_path=lambda *_: True,
            clock=lambda: self.now[0],
        )
        self.assertEqual(engine.status(self.identity).state, State.UNKNOWN)


class TransferFailureTests(CoreCase):
    def test_partial_transfer_preserves_previous_complete_version(self):
        old = self.complete()
        self.new_version()
        self.backend.fail_after = 1
        with self.assertRaises(OSError):
            self.controller.download(self.identity)
        self.assertEqual(self.status().state, State.ERROR)
        self.assertEqual(self.store.copy(self.identity), old)
        self.assertEqual((self.disk / old.object_path).read_bytes(), self.content)

    def test_truncated_stream_is_not_published(self):
        self.backend.files[self.identity] = (self.remote, self.content[:-1])
        with self.assertRaises(IntegrityError):
            self.controller.download(self.identity)
        self.assertIsNone(self.store.copy(self.identity))
        self.assertEqual(self.status().state, State.ERROR)

    def test_overlong_stream_is_not_published(self):
        self.backend.files[self.identity] = (self.remote, self.content + b"extra")
        with self.assertRaises(IntegrityError):
            self.controller.download(self.identity)
        self.assertIsNone(self.store.copy(self.identity))

    def test_matching_size_with_wrong_checksum_is_not_published(self):
        self.backend.files[self.identity] = (self.remote, b"X" * len(self.content))
        with self.assertRaises(IntegrityError):
            self.controller.download(self.identity)
        self.assertIsNone(self.store.copy(self.identity))

    def test_disk_full_keeps_old_file_and_never_succeeds(self):
        old = self.complete()
        self.new_version()
        with patch("cloudmarks.paths.os.write", side_effect=OSError(errno.ENOSPC, "Artificial full disk")):
            with self.assertRaises(OSError):
                self.controller.download(self.identity)
        self.assertEqual(self.store.copy(self.identity), old)
        self.assertEqual((self.disk / old.object_path).read_bytes(), self.content)
        self.assertFalse(self.status().green)

    def test_remote_changes_during_download_preserve_old_snapshot(self):
        for scenario in ("revision", "rename"):
            with self.subTest(scenario=scenario):
                identity = FileIdentity("test-drive", "race-" + scenario)
                original = self.backend.put(identity, f"races/{scenario}.txt", self.content)
                old = self.controller.download(identity)
                changed = []

                def race(index, identity=identity, original=original, scenario=scenario, changed=changed):
                    if index == 0 and not changed:
                        self.now[0] += 1
                        self.backend.put(
                            identity,
                            "races/renamed.txt" if scenario == "rename" else original.relative_path,
                            self.content if scenario == "rename" else b"A different remote version.",
                            original.revision if scenario == "rename" else "fixture-v2",
                        )
                        changed.append(True)

                self.backend.chunk_hook = race
                with self.assertRaises(IntegrityError):
                    self.controller.download(identity)
                self.assertEqual(self.store.copy(identity), old)
                self.assertEqual((self.disk / old.object_path).read_bytes(), self.content)
                self.assertFalse(self.engine.status(identity).green)
                self.backend.chunk_hook = None

    def test_state_failure_after_file_publication_rolls_back_old_reference(self):
        old = self.complete()
        self.new_version()
        self.store.connection.execute("""CREATE TRIGGER fail_completion BEFORE UPDATE ON copies
            BEGIN SELECT RAISE(ABORT, 'Artificial interrupted state write'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.controller.download(self.identity)
        self.assertEqual(self.store.copy(self.identity), old)
        self.assertEqual((self.disk / old.object_path).read_bytes(), self.content)
        self.assertEqual(len(list((self.disk / "objects").glob("*.blob"))), 2)
        self.assertFalse(self.status().green)
        self.store.connection.execute("DROP TRIGGER fail_completion")
        self.controller.download(self.identity)
        self.assertTrue(self.status().green)

    def test_failed_first_transfer_has_no_local_success(self):
        self.backend.fail_after = 0
        with self.assertRaises(OSError):
            self.controller.download(self.identity)
        self.assertIsNone(self.store.copy(self.identity))
        self.assertEqual(self.status().state, State.ERROR)

    def test_arbitrary_backend_cannot_be_attached_to_fixture_controller(self):
        with self.assertRaises(ScopeError):
            FixtureController(self.config, self.store, object())


class StorageAndScopeTests(CoreCase):
    def test_absent_disk_and_replaced_marker_are_unknown(self):
        self.complete()
        marker = self.disk / MARKER
        marker.unlink()
        self.assertEqual(self.status().state, State.UNKNOWN)
        marker.write_text("different-volume")
        self.assertEqual(self.status().state, State.UNKNOWN)

    def test_removed_storage_directory_does_not_use_empty_mountpoint(self):
        self.complete()
        self.disk.rename(self.root / "detached-disk")
        self.disk.mkdir()
        self.assertEqual(self.status().state, State.UNKNOWN)

    def test_marker_symlink_is_rejected(self):
        outside = self.root / "marker-outside"
        outside.write_text("fixture-volume")
        (self.disk / MARKER).unlink()
        (self.disk / MARKER).symlink_to(outside)
        self.assertEqual(self.status().state, State.UNKNOWN)

    def test_content_symlink_and_hard_link_are_rejected(self):
        copy = self.complete()
        path = self.disk / copy.object_path
        outside = self.root / "outside"
        outside.write_bytes(self.content)
        path.unlink()
        path.symlink_to(outside)
        self.assertEqual(self.status().state, State.UNKNOWN)
        path.unlink()
        os.link(outside, path)
        self.assertEqual(self.status().state, State.UNKNOWN)
        self.assertEqual(outside.read_bytes(), self.content)

    def test_directory_symlink_cannot_escape_even_with_matching_external_bytes(self):
        copy = self.complete()
        objects = self.disk / "objects"
        objects.rename(self.root / "old-objects")
        outside = self.root / "outside"
        outside.mkdir()
        (outside / Path(copy.object_path).name).write_bytes(self.content)
        objects.symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.status().state, State.UNKNOWN)

    def test_ancestor_symlink_in_configured_storage_path_is_rejected(self):
        alias = self.root / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        config = replace(self.config, storage_root=alias / "disk")
        engine = StatusEngine(config, self.store, read_path=lambda *_: True, clock=lambda: self.now[0])
        self.assertEqual(engine.status(self.identity).state, State.UNKNOWN)

    def test_fifo_does_not_block_status(self):
        copy = self.complete()
        path = self.disk / copy.object_path
        path.unlink()
        os.mkfifo(path)
        self.assertEqual(self.status().state, State.UNKNOWN)

    def test_manifest_path_escape_and_partial_name_are_unknown(self):
        copy = self.complete()
        for value in ("../../outside", "objects/.partial-incomplete"):
            with self.subTest(value=value):
                self.store.connection.execute("UPDATE copies SET object_path=?", (value,))
                self.assertEqual(self.status().state, State.UNKNOWN)
        self.assertTrue((self.disk / copy.object_path).exists())

    def test_remote_path_traversal_is_rejected_before_state_write(self):
        for value in (
            "../outside",
            "/absolute",
            "a/../outside",
            "a//b",
            "a/",
            "./a",
            "C:\\outside",
            "file:///outside",
            "bad\x00name",
        ):
            with self.subTest(value=value), self.assertRaises(ScopeError):
                self.store.observe(replace(self.remote, relative_path=value))
        self.assertEqual(self.store.remote(self.identity), self.remote)

    def test_other_account_and_other_root_are_refused(self):
        for remote in (
            replace(self.remote, identity=FileIdentity("personal-drive", "item")),
            replace(self.remote, identity=FileIdentity("unconfigured-drive", "item")),
            replace(self.remote, root_id="unconfigured-root"),
        ):
            with self.subTest(remote=remote), self.assertRaises(ScopeError):
                self.store.observe(remote)
        self.assertEqual(self.engine.status(FileIdentity("personal-drive", "item")).state, State.UNKNOWN)

    def test_case_collisions_refuse_second_identity(self):
        other = replace(
            self.remote,
            identity=FileIdentity("test-drive", "other-item"),
            relative_path="FOLDER/DISPOSABLE.TXT",
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.observe(other)
        self.assertIsNone(self.store.remote(other.identity))

    def test_unicode_and_spaces_are_preserved(self):
        name = "Fotos de viaje/niño 日本語 ☁.txt"
        self.backend.put(self.identity, name, self.content)
        self.controller.download(self.identity)
        self.assertEqual(self.store.remote(self.identity).relative_path, name)
        self.assertTrue(self.status().green)

    def test_old_remote_observation_cannot_roll_back_state(self):
        self.store.observe(self.new_version())
        with self.assertRaises(ScopeError):
            self.store.observe(self.remote)

    def test_competing_revisions_with_same_observation_time_are_refused(self):
        with self.assertRaises(ScopeError):
            self.store.observe(replace(self.remote, revision="competing-version"))


class RecoveryTests(CoreCase):
    def test_complete_copy_and_pin_intent_survive_database_restart(self):
        old = self.complete(pinned=True)
        self.reopen()
        self.assertEqual(self.store.copy(self.identity), old)
        self.assertEqual(self.status().state, State.KEPT_OFFLINE)

    def test_interrupted_operation_does_not_become_complete_on_restart(self):
        self.store.begin_transfer(self.identity)
        self.reopen()
        self.assertEqual(self.status().state, State.ERROR)
        self.assertIsNone(self.store.copy(self.identity))

    def test_second_writer_is_refused(self):
        with self.assertRaises(StorageError):
            StateStore(self.config)

    def test_database_cannot_silently_switch_account_root_disk_or_case_policy(self):
        self.store.close()
        for changes in (
            {"allowed_root_id": "other-root"},
            {"volume_id": "other-volume"},
            {"expected_drive_id": "other-drive"},
            {"case_sensitive": True},
        ):
            with self.subTest(changes=changes), self.assertRaises(ScopeError):
                StateStore(replace(self.config, **changes))

    def test_unsupported_schema_is_not_overwritten(self):
        self.store.connection.execute("PRAGMA user_version=99")
        self.store.close()
        with self.assertRaises(ConfigurationError):
            StateStore(self.config)
        with closing(sqlite3.connect(self.config.state_path)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 99)

    def test_process_crash_inside_sqlite_transaction_preserves_old_copy(self):
        old = self.complete(pinned=True)
        self.store.close()
        program = """import os,sqlite3,sys
c=sqlite3.connect(sys.argv[1],isolation_level=None)
c.execute('BEGIN IMMEDIATE')
c.execute("UPDATE copies SET object_path='objects/" + "b"*32 + ".blob'")
os._exit(23)
"""
        result = subprocess.run([sys.executable, "-c", program, str(self.config.state_path)], timeout=10)
        self.assertEqual(result.returncode, 23)
        self.reopen()
        self.assertEqual(self.store.copy(self.identity), old)
        self.assertEqual(self.status().state, State.KEPT_OFFLINE)

    def test_state_file_symlink_is_refused(self):
        self.store.close()
        self.config.state_path.unlink()
        outside = self.root / "outside.sqlite3"
        outside.write_bytes(b"do not overwrite")
        self.config.state_path.symlink_to(outside)
        with self.assertRaises(OSError):
            StateStore(self.config)
        self.assertEqual(outside.read_bytes(), b"do not overwrite")

    def test_nonprivate_state_directory_is_refused(self):
        self.store.close()
        self.private.chmod(0o755)
        with self.assertRaises(StorageError):
            StateStore(self.config)


class ConfigurationTests(unittest.TestCase):
    def test_example_configuration_loads_without_connecting_to_any_account(self):
        value = load_config(Path(__file__).resolve().parents[1] / "examples/config.toml")
        self.assertEqual(value.expected_drive_id, "fixture-test-drive")

    def test_config_rejects_unknown_fields_unsupported_version_and_wrong_types(self):
        template = (Path(__file__).resolve().parents[1] / "examples/config.toml").read_text()
        invalid = (
            template.replace("version = 1", "version = 2"),
            template.replace("version = 1", "version = 1.0"),
            template.replace("version = 1", "version = true"),
            template + '\nsecret = "unexpected"\n',
            template.replace("max_age_seconds = 300", "max_age_seconds = nan"),
            template.replace("max_age_seconds = 300", "max_age_seconds = 0"),
            template.replace("case_sensitive = false", "case_sensitive = 0"),
            template.replace('blocked_drive_ids = ["fixture-personal-drive"]', 'blocked_drive_ids = "drive"'),
            template.replace(
                'expected_drive_id = "fixture-test-drive"', 'expected_drive_id = "fixture-personal-drive"'
            ),
            template.replace('root = "/selected-data-disk/cloudmarks-store"', 'root = "relative/path"'),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            for index, contents in enumerate(invalid):
                path.write_text(contents)
                with self.subTest(index=index), self.assertRaises(ConfigurationError):
                    load_config(path)

    def test_blocked_drive_list_is_optional(self):
        template = (Path(__file__).resolve().parents[1] / "examples/config.toml").read_text()
        line = 'blocked_drive_ids = ["fixture-personal-drive"]'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            for contents in (template.replace(line, "blocked_drive_ids = []"), template.replace(line, "")):
                path.write_text(contents)
                value = load_config(path)
                self.assertEqual(value.blocked_drive_ids, ())
                with self.assertRaises(ScopeError):
                    value.validate_identity(FileIdentity("fixture-personal-drive", "item"))

    def test_missing_configuration_is_a_clean_configuration_error(self):
        with self.assertRaises(ConfigurationError):
            load_config("/nonexistent/cloudmarks-fixture/config.toml")

    def test_only_hex_personal_drive_ids_are_case_normalized(self):
        self.assertEqual(FileIdentity("aabbccddeeff0011", "item"), FileIdentity("AABBCCDDEEFF0011", "item"))
        self.assertNotEqual(FileIdentity("b!OpaqueID", "item"), FileIdentity("b!opaqueid", "item"))

    def test_paths_reject_surrogates_oversized_components_and_traversal(self):
        for value in ("x/../y", "x/./y", "", "a" * 256, "☁" * 86, "bad\ud800name"):
            with self.subTest(value=repr(value)), self.assertRaises(ScopeError):
                relative_parts(value)


if __name__ == "__main__":
    unittest.main()
