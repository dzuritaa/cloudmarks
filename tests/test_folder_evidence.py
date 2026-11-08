"""Single-file and folder badges must apply the same content evidence policy."""

import hashlib
import os
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from cloudmarks.activity import Activity
from cloudmarks.folders import FolderObserver
from cloudmarks.model import IntegrityError, VerificationPending, VerificationRequired
from cloudmarks.paths import MARKER, SecureRoot
from cloudmarks.verification import INLINE_LIMIT, HashBudget, HashMemo, hash_file, status_engine, verify
from tests import test_tracking


class FolderEvidenceTests(test_tracking.TrackingCase):
    def setUp(self):
        super().setUp()
        self.backend.records = {k: v for k, v in self.backend.records.items() if k in {"folder", "file"}}
        self.now = time.time()
        self.clock = lambda: self.now
        for target in (
            "cloudmarks.folders.attest_view",
            "cloudmarks.verification.attest_view",
            "cloudmarks.view.attest_view",
        ):
            patcher = patch(target, return_value=True)
            patcher.start()
            self.addCleanup(patcher.stop)

    def prepare(self, size=100):
        self.backend.records["file"] = (self.path, b"x" * size, "v1", False)
        self.backend.chunks = lambda observation, **kwargs: (
            self.backend.records["file"][1][offset : offset + 1024 * 1024]
            for offset in range(0, size, 1024 * 1024)
        )
        self.tracker.queue_tree("folder")
        self.tracker.resume()
        self.now = time.time()
        self.observer = FolderObserver(
            self.config, self.settings, Activity, backend=self.backend, clock=self.clock
        )
        # Inject deterministic monotonic time without changing the observer API.
        self.observer.memo = HashMemo(monotonic=self.clock)
        self.engine = status_engine(
            self.config, self.store, self.settings, clock=self.clock, memo=HashMemo(monotonic=self.clock)
        )

    def folder_state(self):
        try:
            return self.observer.check("folder", "offline")[0]["state"]
        except (VerificationRequired, VerificationPending):
            return "unknown"
        except IntegrityError:
            return "error"

    def assert_states(self, expected):
        self.assertEqual(self.engine.status(self.identity).state.value, expected)
        self.assertEqual(self.folder_state(), expected)

    def test_shared_behavior_matrix(self):
        for case in (
            "small verified",
            "large without proof",
            "large with proof",
            "changed file",
            "expired rehash",
        ):
            with self.subTest(case=case):
                fixture = FolderEvidenceTests("test_shared_behavior_matrix")
                try:
                    fixture.setUp()
                    fixture.check_matrix_case(case)
                finally:
                    fixture.doCleanups()

    def check_matrix_case(self, case):
        large = case.startswith("large")
        self.prepare(INLINE_LIMIT + 1024 if large else 100)
        if case == "large with proof":
            verify(self.controller, self.path, clock=self.clock)
        if case == "changed file":
            self.assert_states("kept_offline")
            self.visible().chmod(0o600)
            self.visible().write_bytes(b"y" * 100)
        if case == "expired rehash":
            self.assert_states("kept_offline")
            self.now += 120
        with patch("cloudmarks.verification.hash_file", wraps=hash_file) as full:
            expected = {"large without proof": "unknown", "changed file": "error"}.get(case, "kept_offline")
            self.assert_states(expected)
        if large:
            self.assertEqual(full.call_count, 0, "Badges must never hash above INLINE_LIMIT")
        if case == "expired rehash":
            self.assertEqual(full.call_count, 4, "Both paths must rehash both retained copies")

    def test_folder_hash_byte_budget_progresses_on_retry(self):
        self.prepare(100)
        from cloudmarks import folders

        with patch.object(folders, "CHECK_BYTES", 100):
            self.assertEqual(self.folder_state(), "unknown")
            self.assertEqual(self.folder_state(), "kept_offline")

    def test_folder_time_budget_stops_before_hashing(self):
        self.prepare(100)
        with (
            patch("cloudmarks.folders.time.monotonic", side_effect=[0, 1, 2, 3, 4]),
            patch("cloudmarks.verification.hash_file", side_effect=AssertionError("budget exceeded")),
        ):
            self.assertEqual(self.folder_state(), "unknown")

    def test_folder_remembers_failed_fingerprint_until_rehash(self):
        self.prepare(100)
        self.visible().chmod(0o600)
        self.visible().write_bytes(b"y" * 100)
        with patch("cloudmarks.verification.hash_file", wraps=hash_file) as full:
            self.assert_states("error")
            count = full.call_count
            self.assert_states("error")
            self.assertEqual(full.call_count, count)
            self.now += 61
            self.assert_states("error")
            self.assertGreater(full.call_count, count)

    def test_large_proof_deadline_bounds_folder_lease(self):
        self.prepare(INLINE_LIMIT + 1024)
        verify(self.controller, self.path, clock=self.clock)
        # Refresh metadata late in the proof's lifetime.
        self.now += 299.9
        self.store.observe(replace(self.store.remote(self.identity), observed_at=self.now))
        self.observer.trees.clear()
        result, deadline = self.observer.check("folder", "offline")
        self.assertEqual(result["state"], "kept_offline")
        self.assertLessEqual(deadline - self.now, 0.101)


class HashBudgetTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name)
        (self.path / MARKER).write_text("fixture-volume")
        self.content = b"x" * (3 * 1024 * 1024)
        self.sha = hashlib.sha256(self.content).hexdigest()
        (self.path / "file").write_bytes(self.content)

    def test_time_budget_interrupts_between_reads_without_memoizing_success(self):
        ticks = iter([0, 0, 0, 1])
        budget = HashBudget(len(self.content), 0.15, monotonic=lambda: next(ticks))
        memo = HashMemo()
        with (
            SecureRoot(self.path, "fixture-volume") as root,
            patch("cloudmarks.verification.os.read", wraps=os.read) as reads,
        ):
            with self.assertRaises(VerificationPending):
                memo.verify(root, "file", len(self.content), self.sha, budget=budget)
        self.assertEqual([call.args[1] for call in reads.call_args_list], [1024 * 1024])
        self.assertEqual(memo.entries, {})
        self.assertEqual(memo.failures, {})

    def test_growing_file_cannot_exceed_reserved_bytes(self):
        original = os.read
        total = 0

        def growing(fd, size):
            nonlocal total
            data = original(fd, size)
            total += len(data)
            with (self.path / "file").open("ab") as stream:
                stream.write(b"appended")
            return data

        with (
            SecureRoot(self.path, "fixture-volume") as root,
            patch("cloudmarks.verification.os.read", side_effect=growing),
        ):
            with self.assertRaises(IntegrityError):
                hash_file(root, "file", len(self.content), self.sha, HashBudget(len(self.content), 10))
        self.assertEqual(total, len(self.content))

    def test_short_read_is_not_a_complete_hash(self):
        with (
            SecureRoot(self.path, "fixture-volume") as root,
            patch("cloudmarks.verification.os.read", return_value=b""),
        ):
            with self.assertRaises(IntegrityError):
                hash_file(root, "file", len(self.content), self.sha, HashBudget(len(self.content), 10))
