"""Attestation and mount lifecycle with fake processes, never FUSE or an account."""

import json
import signal
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from cloudmarks import view
from cloudmarks.install import atomic_json
from cloudmarks.model import ScopeError, StorageError


class ViewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cm-view-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.config = SimpleNamespace(
            storage_root=self.base / "disk", volume_id="volume", state_path=self.base / "state.sqlite3"
        )
        self.settings = {
            "visible_root": str(self.base / "visible"),
            "rclone_config": "/fake/conf",
            "rclone": "/fake/rclone",
        }
        self.current = {
            "id": "123",
            "source": view.device_name(self.config),
            "options": {"ro"},
            "type": "fuse.rclone",
        }
        atomic_json(
            self.base / "view.json",
            {"mount_id": "123", "volume": "volume", "storage": str(self.config.storage_root)},
        )

    def test_attestation_requires_every_identity_and_readonly_field(self):
        with (
            patch.object(view, "check_volume") as volume,
            patch.object(view, "mounted_view", return_value=self.current),
        ):
            self.assertTrue(view.attest_view(self.config, self.settings))
            volume.assert_called_once_with(self.config, self.settings)
            self.assertTrue(view.attest_view(self.config, self.settings, volume_reuse_seconds=10))
            volume.assert_called_with(self.config, self.settings, reuse_seconds=10)
            for field, bad in [
                ("source", "foreign"),
                ("options", {"rw"}),
                ("id", "replaced"),
                ("type", "ext4"),
            ]:
                with (
                    self.subTest(field=field),
                    patch.object(view, "mounted_view", return_value=dict(self.current, **{field: bad})),
                ):
                    self.assertFalse(view.attest_view(self.config, self.settings))
            for field in ("mount_id", "volume", "storage"):
                with patch.object(view, "read_private_json", return_value={field: "wrong"}):
                    self.assertFalse(view.attest_view(self.config, self.settings))
            with patch.object(view, "mounted_view", return_value=None):
                self.assertFalse(view.attest_view(self.config, self.settings))
            volume.side_effect = StorageError("Disk absent")
            with self.assertRaises(StorageError):
                view.attest_view(self.config, self.settings)

    def test_prove_view_never_opens_union_content_and_revalidates_local_copy(self):
        remote, copy = SimpleNamespace(relative_path="folder/file"), SimpleNamespace(size=7, sha256="hash")
        with patch.object(view, "attest_view", return_value=False), patch.object(view, "SecureRoot") as root:
            self.assertFalse(view.prove_view(self.config, self.settings, remote, copy))
            root.assert_not_called()
        verifier = Mock()
        with patch.object(view, "attest_view", return_value=True), patch.object(view, "SecureRoot") as root:
            self.assertTrue(view.prove_view(self.config, self.settings, remote, copy, verify=verifier))
            verifier.assert_called_once_with(
                root.return_value.__enter__.return_value, "local/folder/file", 7, "hash"
            )
            root.assert_called_once_with(self.config.storage_root, "volume")
            root.return_value.__enter__.return_value.revalidate.assert_called_with("volume")

    def fake_mount(self, *, offline=True, mount=None, process=None, unmount_code=0):
        stack = ExitStack()
        self.addCleanup(stack.close)

        def stub(name, **kwargs):
            return stack.enter_context(patch.object(view, name, **kwargs))

        self.volume = stub("check_volume")
        self.unmounted = stub("require_unmounted")
        self.transport = stub("OneDrive")
        self.store = stub("StateStore").return_value.__enter__.return_value
        self.store.connection.execute.return_value.fetchall.return_value = [("drive", "item")]
        self.controller = stub("OfflineController").return_value
        stub("SecureRoot")
        stub("clean_environment", return_value={})
        stub("time", wraps=view.time).sleep = lambda seconds: None
        self.process = process or Mock(returncode=0)
        self.process.poll.return_value = None
        mounted = [True]
        stub("mounted_view", side_effect=mount or (lambda *args: self.current if mounted[0] else None))
        stack.enter_context(patch.object(view.subprocess, "Popen", return_value=self.process))

        def unmount(*args, **kwargs):
            if not unmount_code:
                mounted[0] = False
            return SimpleNamespace(returncode=unmount_code)

        self.unmount = stack.enter_context(patch.object(view.subprocess, "run", side_effect=unmount))
        self.tracker = stack.enter_context(patch("cloudmarks.tracking.ChangeTracker"))
        return lambda **kwargs: view.mount(
            self.config, self.settings, offline=offline, stop=lambda: True, **kwargs
        )

    def test_offline_mount_has_no_remote_credentials_and_cleans_runtime_file(self):
        run = self.fake_mount()

        def ready():
            conf = next(self.base.glob("offline-view-*.conf"))
            text = conf.read_text()
            self.assertIn("type = alias", text)
            self.assertNotIn("token", text)
            self.assertEqual(conf.stat().st_mode & 0o777, 0o600)

        run(ready=ready)
        self.transport.return_value.authenticate.assert_not_called()
        self.tracker.assert_not_called()
        self.controller.recover.assert_called_once()
        self.controller.verify_entry.assert_called_once()
        self.unmount.assert_called_once()
        self.assertFalse(list(self.base.glob("offline-view-*.conf")))
        self.assertTrue(json.loads((self.base / "view.json").read_text())["offline"])

    def test_online_mount_authenticates_and_checks_revisions_before_start(self):
        run = self.fake_mount(offline=False)
        run()
        self.transport.return_value.authenticate.assert_called_once()
        self.tracker.return_value.check_online.assert_called_once()
        self.assertFalse(json.loads((self.base / "view.json").read_text())["offline"])

    def test_nonempty_or_shared_mountpoint_refuses_without_starting_process(self):
        path = Path(self.settings["visible_root"])
        path.mkdir(mode=0o700)
        run = self.fake_mount()
        (path / "pending").write_text("preserve")
        with self.assertRaises(ScopeError):
            run()
        (path / "pending").unlink()
        path.chmod(0o755)
        with self.assertRaises(ScopeError):
            run()

    def test_failed_mount_and_wrong_readonly_policy_preserve_foreign_mount(self):
        run = self.fake_mount(mount=lambda *a: dict(self.current, source="foreign"))
        with self.assertRaises(StorageError):
            run()
        self.unmount.assert_not_called()
        self.process.poll.return_value = 1
        # Simulate early rclone exit before a mount appears.
        with patch.object(view, "mounted_view", return_value=None):
            with self.assertRaisesRegex(StorageError, "failed to mount"):
                run()

    def test_busy_unmount_is_actionable_and_preserves_runtime_config(self):
        run = self.fake_mount(unmount_code=1)
        with self.assertRaisesRegex(StorageError, "close its open files"):
            run()
        self.assertTrue(list(self.base.glob("offline-view-*.conf")))

    def test_process_exit_and_termination_timeout_are_reported(self):
        run = self.fake_mount()
        self.process.poll.side_effect = [1, 1, 1]
        self.process.returncode = 2
        with self.assertRaisesRegex(StorageError, "exited"):
            run()
        run = self.fake_mount()
        self.process.wait.side_effect = [subprocess.TimeoutExpired("fake", 5), None]
        run()
        self.process.send_signal.assert_called_once_with(signal.SIGTERM)
