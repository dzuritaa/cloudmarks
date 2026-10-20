"""M3 acceptance: durable publication, recovery, cancellation and local eviction."""

import base64
import errno
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from cloudmarks.config import Config
from cloudmarks.engine import StatusEngine
from cloudmarks.model import FileIdentity, IntegrityError, ScopeError, State, StorageError
from cloudmarks.offline import OfflineController, usage
from cloudmarks.onedrive import Cancelled, Observation, QuickXor
from cloudmarks.paths import MARKER, SecureRoot
from cloudmarks.state import ReadStore, StateStore


class Backend:
    def __init__(self, config):
        self.config = config
        self.content = b"Complete artificial data.\n" * 1024
        self.revision = "fixture-v1"
        self.path = "folder/disposable.txt"
        self.item = "stable-fixture-item"
        self.digest_override = None
        self.hook = lambda index: None
        self.describe_hook = lambda count: None
        self.calls = 0

    def describe(self, path):
        from cloudmarks.model import RemoteFile

        self.calls += 1
        self.describe_hook(self.calls)
        remote = RemoteFile(
            FileIdentity(self.config.expected_drive_id, self.item),
            self.config.allowed_root_id,
            self.path,
            self.revision,
            len(self.content),
            time.time(),
        )
        value = QuickXor()
        value.update(self.content)
        return Observation(remote, "quickXorHash", self.digest_override or value.digest())

    def chunks(self, observation, *, cancelled=lambda: False):
        for index, offset in enumerate(range(0, len(self.content), 4096)):
            self.hook(index)
            yield self.content[offset : offset + 4096]


class OfflineCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cm-m3-", dir=os.environ.get("CLOUDMARKS_TEST_TMPDIR"))
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.disk = self.root / "disk"
        self.private = self.root / "state"
        self.disk.mkdir(mode=0o700)
        self.private.mkdir(mode=0o700)
        (self.disk / MARKER).write_text("test-volume\n")
        for name in ("objects", "local", "recovery"):
            (self.disk / name).mkdir()
        self.config = Config(
            "test-drive",
            "allowed-root",
            ("blocked-personal",),
            self.disk,
            "test-volume",
            self.private / "state.sqlite3",
            case_sensitive=True,
        )
        self.settings = {
            "budget_bytes": 32 * 1024 * 1024,
            "min_free_bytes": 0,
            "visible_root": str(self.root / "view"),
        }
        self.store = StateStore(self.config)
        self.addCleanup(lambda: self.store.close())
        self.backend = Backend(self.config)
        self.events = []
        self.controller = OfflineController(
            self.config,
            self.store,
            self.backend,
            self.settings,
            volume_check=lambda *args: None,
            unmounted=lambda *args: None,
            progress=lambda **values: self.events.append(values),
        )
        self.path = self.backend.path
        self.identity = FileIdentity(self.config.expected_drive_id, self.backend.item)

    def keep(self):
        return self.controller.keep(self.path)

    def visible(self):
        return self.disk / "local" / self.path

    def next_version(self):
        self.backend.revision = "fixture-v2"
        self.backend.content = b"New complete version.\n" * 2048

    def reopen(self):
        self.store.close()
        self.store = StateStore(self.config)
        self.controller.store = self.store


class ControlsTests(OfflineCase):
    def test_verified_bytes_pin_progress_and_restart(self):
        copy = self.keep()
        self.assertEqual(self.visible().read_bytes(), self.backend.content)
        self.assertEqual(self.store.copy(self.identity), copy)
        self.assertTrue(self.store.row(self.identity)["pinned"])
        self.assertEqual(self.events[0]["completed"], 0)
        self.assertEqual(self.events[-1]["phase"], "complete")
        self.assertEqual(self.events[-1]["completed"], len(self.backend.content))
        self.assertEqual(self.visible().stat().st_mode & 0o777, 0o444)
        self.reopen()
        self.controller.recover()
        self.assertTrue(self.store.row(self.identity)["pinned"])
        self.assertEqual(self.visible().read_bytes(), self.backend.content)
        engine = StatusEngine(
            self.config,
            self.store,
            read_path=lambda remote, copy: self.visible().read_bytes() == self.backend.content,
        )
        self.assertEqual(engine.status(self.identity).state, State.KEPT_OFFLINE)

    def test_free_requires_unpin_and_preserves_remote_content_and_revision(self):
        copy = self.keep()
        before = self.backend.content, self.backend.revision, self.backend.item
        with self.assertRaises(ScopeError):
            self.controller.free(self.path)
        self.controller.unpin(self.path)
        self.assertTrue(self.visible().exists())
        self.controller.free(self.path)
        self.assertFalse(self.visible().exists())
        self.assertFalse((self.disk / copy.object_path).exists())
        self.assertIsNone(self.store.copy(self.identity))
        self.assertEqual(before, (self.backend.content, self.backend.revision, self.backend.item))
        self.assertFalse(self.store.row(self.identity)["pinned"])

    def test_open_raw_descriptor_keeps_its_bytes_after_local_eviction(self):
        self.keep()
        self.controller.unpin(self.path)
        with self.visible().open("rb") as reader:
            self.controller.free(self.path)
            self.assertEqual(reader.read(), self.backend.content)
        self.assertFalse(self.visible().exists())

    def test_update_preserves_previous_version_in_recovery_storage(self):
        previous = self.keep()
        old = self.visible().read_bytes()
        self.next_version()
        self.keep()
        self.assertEqual(self.visible().read_bytes(), self.backend.content)
        self.assertEqual((self.disk / previous.object_path).read_bytes(), old)
        backups = list((self.disk / "recovery").glob("*.previous"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), old)

    def test_checksum_failure_keeps_old_copy_and_visible_bytes(self):
        copy = self.keep()
        old = self.visible().read_bytes()
        self.next_version()
        self.backend.digest_override = bytes(20)
        with self.assertRaises(IntegrityError):
            self.keep()
        self.assertEqual(self.store.copy(self.identity), copy)
        self.assertEqual(self.visible().read_bytes(), old)
        self.assertEqual(self.store.row(self.identity)["operation"], "error")
        self.assertTrue(list((self.disk / "objects").glob(".partial-*")))

    def test_cancel_retains_pin_and_previous_bytes_then_retry_completes(self):
        copy = self.keep()
        old = self.visible().read_bytes()
        self.next_version()
        cancelled = [False]
        self.backend.hook = lambda index: cancelled.__setitem__(0, True) if index == 1 else None
        self.controller.cancelled = lambda: cancelled[0]
        with self.assertRaises(Cancelled):
            self.keep()
        self.assertEqual(self.store.copy(self.identity), copy)
        self.assertEqual(self.visible().read_bytes(), old)
        self.assertTrue(self.store.row(self.identity)["pinned"])
        self.controller.cancelled = lambda: False
        self.backend.hook = lambda index: None
        self.keep()
        self.assertEqual(self.visible().read_bytes(), self.backend.content)

    def test_changed_revision_during_download_and_during_visible_copy_are_refused(self):
        copy = self.keep()
        original = self.visible().read_bytes()
        for change_at in (2, 3):
            self.next_version()
            self.backend.calls = 0
            self.backend.describe_hook = lambda count, change_at=change_at: (
                setattr(self.backend, "revision", "changed-" + str(change_at)) if count == change_at else None
            )
            with self.assertRaises(IntegrityError):
                self.keep()
            self.assertEqual(self.store.copy(self.identity), copy)
            self.assertEqual(self.visible().read_bytes(), original)

    def test_cancel_during_final_metadata_check_prevents_publication(self):
        old = self.keep()
        content = self.visible().read_bytes()
        self.next_version()
        stopped = [False]
        self.backend.calls = 0
        self.backend.describe_hook = lambda count: stopped.__setitem__(0, True) if count == 3 else None
        self.controller.cancelled = lambda: stopped[0]
        with self.assertRaises(Cancelled):
            self.keep()
        self.assertEqual(self.store.copy(self.identity), old)
        self.assertEqual(self.visible().read_bytes(), content)

    def test_cancel_during_final_eviction_metadata_check_preserves_data(self):
        copy = self.keep()
        self.controller.unpin(self.path)
        stopped = [False]
        self.backend.calls = 0
        self.backend.describe_hook = lambda count: stopped.__setitem__(0, True) if count == 2 else None
        self.controller.cancelled = lambda: stopped[0]
        with self.assertRaises(Cancelled):
            self.controller.free(self.path)
        self.assertEqual(self.store.copy(self.identity), copy)
        self.assertEqual(self.visible().read_bytes(), self.backend.content)

    def test_wrong_identity_or_root_is_never_adopted(self):
        copy = self.keep()
        original = self.backend.describe

        def wrong(path):
            value = original(path)
            return replace(value, remote=replace(value.remote, root_id="outside-root"))

        self.backend.describe = wrong
        with self.assertRaises(ScopeError):
            self.keep()
        self.assertEqual(self.store.copy(self.identity), copy)

    def test_unmanaged_visible_file_is_never_overwritten(self):
        self.visible().parent.mkdir()
        self.visible().write_bytes(b"Unsent user data")
        with self.assertRaises(ScopeError):
            self.keep()
        self.assertEqual(self.visible().read_bytes(), b"Unsent user data")

    def test_dirty_changed_deleted_or_offline_content_is_not_evictable(self):
        self.keep()
        self.controller.unpin(self.path)
        self.store.set_dirty(self.identity, True)
        with self.assertRaises(ScopeError):
            self.controller.free(self.path)
        self.store.set_dirty(self.identity, False)
        self.backend.revision = "remote-newer"
        with self.assertRaises(IntegrityError):
            self.controller.free(self.path)
        self.backend.revision = "fixture-v1"
        with (
            patch.object(self.backend, "describe", side_effect=StorageError("Offline")),
            self.assertRaises(StorageError),
        ):
            self.controller.free(self.path)
        self.visible().chmod(0o600)
        self.visible().write_bytes(b"Unsent edits")
        with self.assertRaises(IntegrityError):
            self.controller.free(self.path)
        self.assertEqual(self.visible().read_bytes(), b"Unsent edits")

    def test_budget_counts_two_copies_and_retained_partial_data(self):
        self.settings["budget_bytes"] = len(self.backend.content) + 65536
        with self.assertRaises(StorageError):
            self.keep()
        self.assertFalse(self.visible().exists())
        self.assertIsNone(self.store.copy(self.identity))
        self.settings["budget_bytes"] = 1024 * 1024
        (self.disk / "objects/.partial-leftover").write_bytes(b"x" * (1024 * 1024))
        with self.assertRaises(StorageError):
            self.keep()

    def test_free_space_failure_and_mid_write_disk_full_preserve_old_version(self):
        old = self.keep()
        original = self.visible().read_bytes()
        self.next_version()
        self.settings["min_free_bytes"] = 2**62
        with self.assertRaises(StorageError):
            self.keep()
        self.settings["min_free_bytes"] = 0
        with (
            patch("cloudmarks.offline.os.write", side_effect=OSError(errno.ENOSPC, "Artificial full disk")),
            self.assertRaises(OSError),
        ):
            self.keep()
        self.assertEqual(self.store.copy(self.identity), old)
        self.assertEqual(self.visible().read_bytes(), original)

    def test_missing_disk_and_mounted_view_block_controls(self):
        self.controller.unmounted = lambda settings: (_ for _ in ()).throw(StorageError("Mounted view"))
        with self.assertRaises(StorageError):
            self.keep()
        self.assertEqual(self.backend.calls, 0)
        self.controller.unmounted = lambda settings: None
        (self.disk / MARKER).unlink()
        with self.assertRaises(StorageError):
            self.keep()
        self.assertEqual(self.backend.calls, 0)

    def test_symlink_and_hardlink_tampering_cannot_be_published_or_evicted(self):
        copy = self.keep()
        self.controller.unpin(self.path)
        self.visible().unlink()
        self.visible().symlink_to(self.disk / copy.object_path)
        with self.assertRaises((ScopeError, OSError)):
            self.controller.free(self.path)
        self.visible().unlink()
        os.link(self.disk / copy.object_path, self.visible())
        with self.assertRaises(ScopeError):
            self.controller.free(self.path)

    def test_readonly_state_reader_does_not_recover_or_write_and_sees_progress(self):
        self.keep()
        with ReadStore(self.config) as reader:
            self.store.begin_transfer(self.identity)
            self.assertEqual(reader.row(self.identity)["operation"], "active")
            with self.assertRaises(sqlite3.OperationalError):
                reader.set_pin(self.identity, False)
        self.assertEqual(self.store.row(self.identity)["operation"], "active")

    def test_readonly_sqlite_uri_preserves_literal_query_characters(self):
        config = replace(self.config, state_path=self.private / "state%?#.sqlite3")
        with StateStore(config) as writer:
            remote = self.backend.describe(self.path).remote
            writer.observe(remote)
            with ReadStore(config) as reader:
                self.assertEqual(reader.remote(remote.identity), remote)

    def test_publication_exception_rolls_back_at_every_name_change(self):
        old = self.keep()
        content = self.visible().read_bytes()
        for boundary in ("intent", "backup", "visible"):
            self.next_version()
            self.controller.hook = lambda phase, boundary=boundary: (
                (_ for _ in ()).throw(OSError("Injected failure")) if phase == boundary else None
            )
            with self.assertRaises(OSError):
                self.keep()
            self.assertEqual(self.store.copy(self.identity), old)
            self.assertEqual(self.visible().read_bytes(), content)
            self.assertFalse(self.store.connection.execute("SELECT 1 FROM publications").fetchone())

    def test_transaction_failure_after_visible_publication_restores_previous_bytes(self):
        old = self.keep()
        content = self.visible().read_bytes()
        self.next_version()
        with (
            patch.object(
                self.store,
                "_record_complete",
                side_effect=sqlite3.OperationalError("Injected commit failure"),
            ),
            self.assertRaises(sqlite3.OperationalError),
        ):
            self.keep()
        self.assertEqual(self.store.copy(self.identity), old)
        self.assertEqual(self.visible().read_bytes(), content)

    def test_eviction_exception_restores_visible_and_object_files(self):
        old = self.keep()
        for boundary in ("evict-visible", "evict-object"):
            self.store.set_pin(self.identity, False)
            self.controller.hook = lambda phase, boundary=boundary: (
                (_ for _ in ()).throw(OSError("Injected failure")) if phase == boundary else None
            )
            with self.assertRaises(OSError):
                self.controller.free(self.path)
            self.assertEqual(self.store.copy(self.identity), old)
            self.assertEqual(self.visible().read_bytes(), self.backend.content)
            self.assertTrue((self.disk / old.object_path).exists())
            self.store.connection.execute("UPDATE files SET operation='idle'")

    def test_crash_recovery_before_commit_does_not_promote_new_bytes(self):
        old = self.keep()
        original = self.visible().read_bytes()
        self.store.close()
        program = r"""import os,sys
from pathlib import Path
from cloudmarks.config import Config
from cloudmarks.offline import OfflineController
from cloudmarks.state import StateStore
from tests.test_offline import Backend
root=Path(sys.argv[1])
config=Config("test-drive","allowed-root",("blocked-personal",),root/"disk","test-volume",root/"state/state.sqlite3",case_sensitive=True)
with StateStore(config) as store:
    backend=Backend(config); backend.content=b"New crash candidate"*2048; backend.revision="crash-v2"
    settings={"budget_bytes":32*1024*1024,"min_free_bytes":0}
    controller=OfflineController(config,store,backend,settings,volume_check=lambda *args:None,unmounted=lambda *args:None,
                                 hook=lambda phase:os._exit(27) if phase==sys.argv[2] else None)
    controller.keep(backend.path)
"""
        for phase in ("intent", "backup", "visible"):
            result = subprocess.run([os.sys.executable, "-c", program, str(self.root), phase], timeout=10)
            self.assertEqual(result.returncode, 27)
            self.reopen()
            self.controller.recover()
            self.assertEqual(self.store.copy(self.identity), old)
            self.assertEqual(self.visible().read_bytes(), original)
            self.store.close()
        self.reopen()

    def test_crash_mid_eviction_restores_both_committed_files(self):
        copy = self.keep()
        self.controller.unpin(self.path)
        self.store.close()
        program = r"""import os,sys
from pathlib import Path
from cloudmarks.config import Config
from cloudmarks.offline import OfflineController
from cloudmarks.state import StateStore
from tests.test_offline import Backend
root=Path(sys.argv[1])
config=Config("test-drive","allowed-root",("blocked-personal",),root/"disk","test-volume",root/"state/state.sqlite3",case_sensitive=True)
with StateStore(config) as store:
    backend=Backend(config)
    controller=OfflineController(config,store,backend,{},volume_check=lambda *args:None,unmounted=lambda *args:None,
                                 hook=lambda phase:os._exit(29) if phase==sys.argv[2] else None)
    controller.free(backend.path)
"""
        for phase in ("evict-visible", "evict-object"):
            result = subprocess.run([os.sys.executable, "-c", program, str(self.root), phase], timeout=30)
            self.assertEqual(result.returncode, 29)
            self.reopen()
            self.controller.recover()
            self.assertEqual(self.store.copy(self.identity), copy)
            self.assertEqual(self.visible().read_bytes(), self.backend.content)
            self.assertEqual((self.disk / copy.object_path).read_bytes(), self.backend.content)
            self.store.connection.execute("UPDATE files SET operation='idle'")
            self.store.close()
        self.reopen()

    def test_usage_accounts_for_allocated_space_not_only_length(self):
        with SecureRoot(self.disk, self.config.volume_id) as root:
            before = usage(root)
            path = self.disk / "objects/orphan"
            path.write_bytes(b"x")
            after = usage(root)
            self.assertGreaterEqual(after - before, max(1, path.stat().st_blocks * 512))


class ChecksumTests(unittest.TestCase):
    def test_quickxor_fixed_vectors_without_external_tools(self):
        for content, expected in (
            (b"", "AAAAAAAAAAAAAAAAAAAAAAAAAAA="),
            (b"abc", "YRDDGAAAAAAAAAAAAwAAAAAAAAA="),
        ):
            value = QuickXor()
            for byte in content:
                value.update(bytes([byte]))
            self.assertEqual(base64.b64encode(value.digest()).decode(), expected)

    def test_streaming_quickxor_length_rotation_and_independent_rclone_vectors(self):
        binary = shutil.which("rclone")
        if not binary:
            self.skipTest("Optional independent rclone checksum vectors unavailable")
        for content in (b"", b"abc", bytes(range(256)), bytes(range(251)) * 1000):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "fixture"
                path.write_bytes(content)
                output = subprocess.check_output(
                    [binary, "hashsum", "QuickXorHash", str(path), "--base64", "--config", "/dev/null"],
                    stderr=subprocess.DEVNULL,
                    text=True,
                )
                expected = output.split()[0]
                value = QuickXor()
                for offset in range(0, len(content), 17):
                    value.update(content[offset : offset + 17])
                self.assertEqual(value.digest(), base64.urlsafe_b64decode(expected))

    def test_observation_flags_and_checksums_are_version_evidence(self):
        from cloudmarks.model import RemoteFile

        remote = RemoteFile(FileIdentity("test", "id"), "root", "f", "v1", 0, time.time())
        original = Observation(remote, "quickXorHash", bytes(20))
        self.assertTrue(
            original.same_version(replace(original, remote=replace(remote, observed_at=time.time())))
        )
        for changed in (
            replace(original, digest=b"x" * 20),
            replace(original, remote=replace(remote, deleted=True)),
            replace(original, remote=replace(remote, is_directory=True)),
        ):
            self.assertFalse(original.same_version(changed))
