"""Status-only reuse of content hashes and storage-volume proofs."""

import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cloudmarks import offline_config
from cloudmarks.model import IntegrityError, StorageError
from cloudmarks.paths import MARKER, SecureRoot
from cloudmarks.verification import HashMemo


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class HashMemoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cm-memo-", dir="/tmp")
        self.root_path = Path(self.temp.name)
        (self.root_path / MARKER).write_text("volume-fixture\n")
        self.content = b"retained bytes\n" * 1000
        self.sha = hashlib.sha256(self.content).hexdigest()
        (self.root_path / "objects").mkdir()
        self.path = self.root_path / "objects/item"
        self.path.write_bytes(self.content)
        self.clock = Clock()
        self.memo = HashMemo(seconds=60, monotonic=self.clock)

    def tearDown(self):
        self.temp.cleanup()

    def verify(self):
        with SecureRoot(self.root_path, "volume-fixture") as root:
            return self.memo.verify(root, "objects/item", len(self.content), self.sha)

    def test_unchanged_file_is_hashed_once_within_the_interval(self):
        with patch.object(SecureRoot, "verify", autospec=True, side_effect=SecureRoot.verify) as full:
            first = self.verify()
            second = self.verify()
        self.assertEqual(first, second)
        self.assertEqual(full.call_count, 1)

    def test_hash_is_repeated_after_the_interval(self):
        with patch.object(SecureRoot, "verify", autospec=True, side_effect=SecureRoot.verify) as full:
            self.verify()
            self.clock.now += 61
            self.verify()
        self.assertEqual(full.call_count, 2)

    def test_a_rewrite_invalidates_the_memo_and_is_detected(self):
        self.verify()
        changed = bytearray(self.content)
        changed[0] ^= 1
        self.path.write_bytes(bytes(changed))
        with self.assertRaises(IntegrityError):
            self.verify()

    def test_a_failed_hash_is_never_remembered(self):
        self.path.write_bytes(b"x" * len(self.content))
        for _ in range(2):
            with self.assertRaises(IntegrityError):
                self.verify()

    def test_memo_size_is_bounded(self):
        memo = HashMemo(seconds=60, limit=2, monotonic=self.clock)
        with SecureRoot(self.root_path, "volume-fixture") as root:
            for index in range(4):
                name = f"objects/item{index}"
                (self.root_path / name).write_bytes(self.content)
                memo.verify(root, name, len(self.content), self.sha)
        self.assertEqual(len(memo.entries), 2)


class VolumeReuseTests(unittest.TestCase):
    def setUp(self):
        offline_config._VOLUME_PROOFS.clear()
        self.temp = tempfile.TemporaryDirectory(prefix="cm-volume-", dir="/tmp")
        self.config = type("Config", (), {"storage_root": Path(self.temp.name)})()
        info = os.stat(self.temp.name)
        self.row = {
            "uuid": "uuid-1",
            "target": "/",
            "fstype": "ext4",
            "options": "rw,relatime",
            "maj:min": f"{os.major(info.st_dev)}:{os.minor(info.st_dev)}",
        }
        self.settings = {"storage_mount_uuid": "uuid-1", "storage_mount_target": "/"}

    def tearDown(self):
        offline_config._VOLUME_PROOFS.clear()
        self.temp.cleanup()

    def findmnt(self, *args, **kwargs):
        import json
        import subprocess

        return subprocess.CompletedProcess(args, 0, json.dumps({"filesystems": [self.row]}).encode(), b"")

    def test_status_reuse_skips_findmnt_but_mutations_never_do(self):
        with (
            patch("cloudmarks.offline_config.subprocess.run", side_effect=self.findmnt) as run,
            patch("cloudmarks.offline_config.check_binding") as binding,
        ):
            offline_config.check_volume(self.config, self.settings, reuse_seconds=10)
            offline_config.check_volume(self.config, self.settings, reuse_seconds=10)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(binding.call_count, 2)
            offline_config.check_volume(self.config, self.settings)
            self.assertEqual(run.call_count, 2)

    def test_failed_check_is_not_reused(self):
        self.row["options"] = "ro"
        with (
            patch("cloudmarks.offline_config.subprocess.run", side_effect=self.findmnt) as run,
            patch("cloudmarks.offline_config.check_binding"),
        ):
            for _ in range(2):
                with self.assertRaises(StorageError):
                    offline_config.check_volume(self.config, self.settings, reuse_seconds=10)
        self.assertEqual(run.call_count, 2)

    def test_binding_failure_drops_the_reused_proof(self):
        with (
            patch("cloudmarks.offline_config.subprocess.run", side_effect=self.findmnt) as run,
            patch("cloudmarks.offline_config.check_binding") as binding,
        ):
            offline_config.check_volume(self.config, self.settings, reuse_seconds=10)
            binding.side_effect = StorageError("other store")
            with self.assertRaises(StorageError):
                offline_config.check_volume(self.config, self.settings, reuse_seconds=10)
            binding.side_effect = None
            offline_config.check_volume(self.config, self.settings, reuse_seconds=10)
        self.assertEqual(run.call_count, 2)


if __name__ == "__main__":
    unittest.main()
