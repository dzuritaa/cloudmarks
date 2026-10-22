"""A stopped cloud mount must not become a silent local upload destination."""

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cloudmarks.model import ConfigurationError, StorageError
from cloudmarks.startup import guard_mountpoint, run


class MountGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cm-startup-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "view"
        self.root.mkdir(mode=0o700)
        self.addCleanup(lambda: self.root.chmod(0o700) if self.root.exists() else None)

    def test_stopped_directory_rejects_accidental_writes_and_can_be_prepared(self):
        guard_mountpoint(self.root, prepare=False)
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o500)
        if os.getuid() != 0:
            with self.assertRaises(PermissionError):
                (self.root / "accidental.txt").write_text("pending")
        guard_mountpoint(self.root, prepare=True)
        (self.root / "intentional.txt").write_text("pending")
        self.assertEqual((self.root / "intentional.txt").read_text(), "pending")

    def test_pending_files_refuse_startup_without_hiding_or_modifying_them(self):
        pending = self.root / "pending.txt"
        pending.write_bytes(b"User file awaiting upload")
        guard_mountpoint(self.root, prepare=False)
        with self.assertRaises(StorageError):
            guard_mountpoint(self.root, prepare=True)
        self.assertEqual(pending.read_bytes(), b"User file awaiting upload")
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o500)

    def test_protection_skips_busy_mount_and_prepare_refuses_it(self):
        with patch("cloudmarks.startup.mounted_view", return_value={"type": "fuse.rclone"}):
            result = guard_mountpoint(self.root, prepare=False)
            self.assertTrue(result["mounted"])
            self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
            with self.assertRaises(StorageError):
                guard_mountpoint(self.root, prepare=True)

    def test_symlink_and_shared_mountpoints_are_rejected(self):
        alias = self.root.parent / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(OSError):
            guard_mountpoint(alias, prepare=True)
        self.root.chmod(0o755)
        with self.assertRaises(StorageError):
            guard_mountpoint(self.root, prepare=True)

    def test_fixture_and_mismatched_profiles_cannot_change_mountpoints(self):
        with self.assertRaises(ConfigurationError):
            run({"mode": "fixture"}, "offline", "prepare")
        settings = {"mode": "onedrive", "core_config": "unused", "visible_root": str(self.root)}
        with patch("cloudmarks.startup.load_offline", return_value=(None, {"visible_root": "/other"})):
            with self.assertRaises(ConfigurationError):
                run(settings, "offline", "prepare")
