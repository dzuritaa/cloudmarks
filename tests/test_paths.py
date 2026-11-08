"""Store-root privacy and durable directory creation in SecureRoot."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cloudmarks.model import StorageError
from cloudmarks.paths import MARKER, PrivateRootError, SecureRoot


class SecureRootTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cm-paths-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "store"
        self.root.mkdir(mode=0o700)
        (self.root / MARKER).write_text("volume\n")

    def test_private_root_opens(self):
        with SecureRoot(self.root, "volume") as root:
            self.assertIsNotNone(root.fd)

    def test_group_or_other_access_is_refused_with_a_fix(self):
        for mode in (0o750, 0o705, 0o755, 0o777):
            self.root.chmod(mode)
            with self.subTest(mode=oct(mode)), self.assertRaises(PrivateRootError) as caught:
                SecureRoot(self.root, "volume")
            self.assertIn("chmod 700", str(caught.exception))
            self.assertIsInstance(caught.exception, StorageError)

    def test_wrong_marker_still_reports_the_generic_storage_error(self):
        (self.root / MARKER).write_text("other\n")
        with self.assertRaises(StorageError) as caught:
            SecureRoot(self.root, "volume")
        self.assertNotIsInstance(caught.exception, PrivateRootError)

    def test_new_directories_are_synced_into_their_parent(self):
        synced = []
        original = os.fsync

        def record(fd):
            synced.append(os.readlink(f"/proc/self/fd/{fd}"))
            return original(fd)

        with SecureRoot(self.root, "volume") as root, patch("cloudmarks.paths.os.fsync", side_effect=record):
            with root.parent("local/a/b/file", create=True):
                pass
            with root.parent("local/a/b/other", create=True):
                pass  # Existing directories need no new sync.
        self.assertEqual(synced, [str(self.root), str(self.root / "local"), str(self.root / "local/a")])


if __name__ == "__main__":
    unittest.main()
