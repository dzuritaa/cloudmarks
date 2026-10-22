"""Cache notifications observe writes without repeatedly walking the cache."""

import json
import tempfile
import unittest
from pathlib import Path

from cloudmarks.activity import cache_entries
from cloudmarks.cache_watch import CacheWatch, Rescan


class CacheWatchTests(unittest.TestCase):
    def test_selected_updates_deletions_metadata_and_directory_rescan(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data, meta = root / "data", root / "meta"
            data.mkdir()
            meta.mkdir()
            (data / "file").write_bytes(b"one")
            (meta / "file").write_text(json.dumps({"Dirty": True}))
            watch = CacheWatch(data, meta)
            try:
                self.assertEqual(watch.changed(), set())
                (data / "file").write_bytes(b"two")
                self.assertEqual(watch.changed(), {"file"})
                self.assertTrue(cache_entries(data, meta, paths={"file"})["file"][1])
                (meta / "file").write_text(json.dumps({"Dirty": False}))
                self.assertEqual(watch.changed(), {"file"})
                self.assertFalse(cache_entries(data, meta, paths={"file"})["file"][1])
                (data / "file").unlink()
                self.assertEqual(watch.changed(), {"file"})
                self.assertEqual(cache_entries(data, meta, paths={"file"}), {})
                (data / "new-folder").mkdir()
                with self.assertRaises(Rescan):
                    watch.changed()
            finally:
                watch.close()
