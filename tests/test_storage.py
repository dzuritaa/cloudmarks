"""Store cleanup must remove only old, unreferenced store files, and only when asked."""

import json
import os
import unittest
from unittest.mock import patch

from cloudmarks import storage
from cloudmarks.model import StorageError
from cloudmarks.onedrive import Cancelled
from cloudmarks.storage import collect
from tests.test_offline import OfflineCase


class CleanupTests(OfflineCase):
    def files(self, area):
        return sorted(p.name for p in (self.disk / area).iterdir())

    def replace_version(self):
        old = self.keep()
        self.next_version()
        new = self.keep()
        return old, new

    def test_report_is_the_default_and_changes_nothing(self):
        old, new = self.replace_version()
        before = (self.files("objects"), self.files("recovery"))
        result = collect(self.controller, min_age_seconds=0)
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["categories"]["superseded"]["eligible_files"], 1)
        self.assertEqual(result["categories"]["recovery"]["eligible_files"], 1)
        self.assertEqual(result["removed_files"], 0)
        self.assertEqual((self.files("objects"), self.files("recovery")), before)

    def test_apply_removes_old_versions_and_keeps_the_current_copy(self):
        old, new = self.replace_version()
        result = collect(self.controller, apply=True, min_age_seconds=0)
        self.assertEqual(result["removed_files"], 2)
        self.assertGreater(result["removed_bytes"], 0)
        self.assertFalse((self.disk / old.object_path).exists())
        self.assertEqual(self.files("recovery"), [])
        self.assertEqual(self.store.copy(self.identity), new)
        from cloudmarks.paths import SecureRoot

        with SecureRoot(self.disk, "test-volume") as root:
            self.controller.verify_entry(root, self.controller.entry(self.identity))
        self.assertIsNone(self.controller.usage_bound)
        self.assertEqual(collect(self.controller, apply=True, min_age_seconds=0)["removed_files"], 0)

    def test_recent_files_are_not_eligible_by_default(self):
        self.replace_version()
        result = collect(self.controller, apply=True)
        self.assertEqual(result["categories"]["superseded"]["files"], 1)
        self.assertEqual(result["categories"]["superseded"]["eligible_files"], 0)
        self.assertEqual(result["removed_files"], 0)

    def test_cancelled_download_partial_is_reported_as_partial(self):
        cancelled = [False]
        self.backend.hook = lambda index: cancelled.__setitem__(0, True) if index == 1 else None
        self.controller.cancelled = lambda: cancelled[0]
        with self.assertRaises(Cancelled):
            self.keep()
        self.controller.cancelled = lambda: False
        result = collect(self.controller, apply=True, min_age_seconds=0)
        self.assertEqual(result["categories"]["partial"]["files"], 1)
        self.assertEqual(result["removed_files"], 1)
        self.assertFalse(any(name.startswith(".partial-") for name in self.files("objects")))

    def test_snapshots_candidates_archives_and_proofs_are_kept(self):
        self.keep()
        for name in ("snap.blob", "candidate.blob", "archived.local", "proof.blob"):
            area = "recovery" if name.endswith(".local") else "objects"
            (self.disk / area / name).write_bytes(b"retained history")
        connection = self.store.connection
        connection.execute(
            "INSERT INTO snapshots VALUES ('objects/snap.blob','test-drive','x','r','p',16,'0',0)"
        )
        connection.execute(
            "INSERT INTO issues VALUES ('test-drive',?,?,?,?,?)",
            (
                self.backend.item,
                "review",
                "r",
                self.path,
                json.dumps({"object_path": "objects/candidate.blob"}),
            ),
        )
        receipt = {"sources": {"local": {"path": "recovery/archived.local"}}}
        connection.execute("INSERT INTO archives VALUES ('a1', ?)", (json.dumps(receipt),))
        proof = {"sources": {"object": {"path": "objects/proof.blob"}}}
        connection.execute(
            "INSERT INTO verifications VALUES ('test-drive', 'other', ?)", (json.dumps(proof),)
        )
        result = collect(self.controller, apply=True, min_age_seconds=0)
        self.assertEqual(result["removed_files"], 0)
        for area, name in (
            ("objects", "snap.blob"),
            ("objects", "candidate.blob"),
            ("objects", "proof.blob"),
            ("recovery", "archived.local"),
        ):
            self.assertTrue((self.disk / area / name).exists(), name)

    def test_unrecovered_publication_blocks_cleanup(self):
        self.keep()
        self.controller.journal(self.identity, {"action": "publish", "backup": "recovery/x.previous"})
        with self.assertRaises(StorageError):
            collect(self.controller, apply=True, min_age_seconds=0)

    def test_links_directories_and_changed_files_are_kept(self):
        self.replace_version()
        (self.disk / "objects/nested").mkdir()
        os.symlink("/etc/passwd", self.disk / "recovery/link")
        original = storage.inventory

        def inventory_then_touch(*args, **kwargs):
            candidates, unexpected = original(*args, **kwargs)
            target = self.disk / candidates[0].path
            target.chmod(0o600)
            with open(target, "ab") as stream:
                stream.write(b"x")
            return candidates, unexpected

        with patch("cloudmarks.storage.inventory", side_effect=inventory_then_touch):
            result = collect(self.controller, apply=True, min_age_seconds=0)
        self.assertEqual(result["unexpected"], 2)
        self.assertEqual(result["changed_since_inventory"], 1)
        self.assertEqual(result["removed_files"], 1)
        self.assertTrue((self.disk / "objects/nested").is_dir())
        self.assertTrue(os.path.islink(self.disk / "recovery/link"))

    def test_wrong_disk_is_refused_before_inventory(self):
        self.controller.volume_check = lambda *a: (_ for _ in ()).throw(StorageError("other disk"))
        with (
            patch("cloudmarks.storage.inventory", side_effect=AssertionError("inventoried")),
            self.assertRaises(StorageError),
        ):
            collect(self.controller, apply=True, min_age_seconds=0)


if __name__ == "__main__":
    unittest.main()
