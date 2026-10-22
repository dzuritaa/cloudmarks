"""Nautilus callback behavior with a minimal GI surface, no desktop required.

The real GI/Files rendering and event loop are checked separately by the
isolated desktop harness; these tests stress expiry and bounded callbacks.
"""

import importlib
import queue
import sys
import time
import types
import unittest
from unittest.mock import patch

from cloudmarks.desktop import MAX_TRACKED, settings_key


class BackgroundFake:
    def __init__(self, path):
        self.outbox = queue.Queue()
        self.submitted = []
        self.notes = []

    def note(self, event, **fields):
        self.notes.append((event, fields))

    def submit(self, uri):
        self.submitted.append(uri)
        return True


class File:
    def __init__(self, uri):
        self.uri = uri
        self.emblems = []
        self.attributes = {}
        self.invalidations = 0
        self.callback = None

    def get_uri(self):
        return self.uri

    def add_emblem(self, emblem):
        self.emblems.append(emblem)

    def add_string_attribute(self, name, value):
        self.attributes[name] = value

    def invalidate_extension_info(self):
        self.invalidations += 1
        self.emblems.clear()
        self.attributes.clear()
        if self.callback:
            self.callback(self)

    def is_gone(self):
        return False


class AdapterTests(unittest.TestCase):
    def setUp(self):
        gi = types.ModuleType("gi")
        gi.require_version = lambda *args: None
        repo = types.ModuleType("gi.repository")
        repo.GObject = types.SimpleNamespace(GObject=type("GObject", (), {}))
        repo.Nautilus = types.SimpleNamespace(
            InfoProvider=type("InfoProvider", (), {}), ColumnProvider=type("ColumnProvider", (), {})
        )
        repo.GLib = types.SimpleNamespace(timeout_add=lambda *args: 1, SOURCE_CONTINUE=True)
        repo.Gdk = types.SimpleNamespace(Display=types.SimpleNamespace(get_default=lambda: None))
        repo.Gtk = types.SimpleNamespace()
        self.modules = patch.dict(sys.modules, {"gi": gi, "gi.repository": repo})
        self.modules.start()
        self.addCleanup(self.modules.stop)
        sys.modules.pop("cloudmarks.nautilus_extension", None)
        self.addCleanup(lambda: sys.modules.pop("cloudmarks.nautilus_extension", None))
        self.module = importlib.import_module("cloudmarks.nautilus_extension")
        self.worker = patch.object(self.module, "Background", BackgroundFake)
        self.worker.start()
        self.addCleanup(self.worker.stop)
        self.adapter = self.module.CloudMarksExtension()
        self.settings = {
            "version": 1,
            "mode": "fixture",
            "enabled": True,
            "visible_root": "/fixture",
            "core_config": "/config/core.toml",
            "socket": "/run/cm/status.sock",
        }
        self.adapter.background.outbox.put(("settings", self.settings))
        self.adapter.drain()

    def file(self, name="document.txt"):
        file = File("file:///fixture/" + name)
        file.callback = self.adapter.update_file_info
        self.adapter.update_file_info(file)
        return file

    def deliver(self, file, state="saved_locally", *, expires=None, valid_for_ms=1000):
        self.adapter.background.outbox.put(
            (
                "status",
                settings_key(self.settings),
                file.uri,
                expires or time.monotonic() + 1,
                {"state": state, "reason": "Fixture evidence", "valid_for_ms": valid_for_ms},
            )
        )
        self.adapter.drain()

    def test_callbacks_only_queue_and_return_immediately(self):
        with (
            patch("socket.socket", side_effect=AssertionError("UI must not connect")),
            patch("builtins.open", side_effect=AssertionError("UI must not open files")),
        ):
            file = self.file()
        self.assertIn("Needs checking", file.attributes["cloudmarks_status"])
        self.assertEqual(self.adapter.background.submitted, [file.uri])
        self.deliver(file)
        self.assertIn("Saved locally", file.attributes["cloudmarks_status"])

    def test_helper_loss_expires_green_and_repaints_unknown(self):
        file = self.file()
        self.deliver(file)
        with patch.object(self.module.time, "monotonic", return_value=time.monotonic() + 2):
            self.adapter.drain()
        self.assertIn("Needs checking", file.attributes["cloudmarks_status"])
        self.assertGreaterEqual(file.invalidations, 2)
        self.assertTrue(any(event == "lease_expired" for event, fields in self.adapter.background.notes))

    def test_late_response_cannot_restore_green(self):
        file = self.file()
        self.deliver(file, expires=time.monotonic() - 1)
        self.assertIn("Needs checking", file.attributes["cloudmarks_status"])
        self.assertEqual(self.adapter.cache[file.uri][1]["state"], "unknown")

    def test_late_helper_result_schedules_retry_after_recovery(self):
        file = self.file()
        self.deliver(file, expires=time.monotonic() - 1)
        before = len(self.adapter.background.submitted)
        with patch.object(self.module.time, "monotonic", return_value=time.monotonic() + 0.6):
            self.adapter.drain()
        self.assertGreater(len(self.adapter.background.submitted), before)

    def test_renewal_keeps_current_evidence_until_its_deadline(self):
        file = self.file()
        self.deliver(file)
        before = len(self.adapter.background.submitted)
        with patch.object(self.module.time, "monotonic", return_value=time.monotonic() + 0.4):
            self.adapter.drain()
        self.assertGreater(len(self.adapter.background.submitted), before)
        self.assertIn("Saved locally", file.attributes["cloudmarks_status"])

    def test_unchanged_evidence_renews_without_clearing_attributes(self):
        file = self.file()
        self.deliver(file)
        before = file.invalidations
        self.deliver(file)
        self.assertEqual(file.invalidations, before)
        self.deliver(file, valid_for_ms=800)
        self.assertEqual(file.invalidations, before)
        self.deliver(file, state="kept_offline")
        self.assertGreater(file.invalidations, before)
        self.assertIn("Kept offline", file.attributes["cloudmarks_status"])

    def test_disable_clears_existing_badges_without_restart(self):
        file = self.file()
        self.deliver(file)
        self.adapter.background.outbox.put(("settings", dict(self.settings, enabled=False)))
        self.adapter.drain()
        self.assertEqual(file.attributes, {})
        self.assertEqual(file.emblems, [])
        self.assertEqual(self.adapter.cache, {})

    def test_configuration_loss_clears_green(self):
        file = self.file()
        self.deliver(file)
        self.adapter.background.outbox.put(("settings", None))
        self.adapter.drain()
        self.assertEqual(file.attributes, {})

    def test_unrelated_folders_never_queued_or_badged(self):
        file = File("file:///fixture-neighbour/document.txt")
        self.adapter.update_file_info(file)
        self.assertEqual(file.attributes, {})
        self.assertEqual(self.adapter.background.submitted, [])

    def test_upload_scope_gets_its_own_status_and_does_no_io_in_callback(self):
        self.settings = dict(self.settings, version=3, mode="onedrive", upload_root="/upload")
        self.adapter.background.outbox.put(("settings", self.settings))
        self.adapter.drain()
        file = File("file:///upload/new.txt")
        file.callback = self.adapter.update_file_info
        with (
            patch("socket.socket", side_effect=AssertionError("UI must not connect")),
            patch("builtins.open", side_effect=AssertionError("UI must not open files")),
        ):
            self.adapter.update_file_info(file)
        self.deliver(file, state="uploading")
        self.assertIn("Uploading (test account, uploads)", file.attributes["cloudmarks_status"])
        self.deliver(file, state="cloud_only")
        self.assertIn("Cloud only", file.attributes["cloudmarks_status"])
        neighbour = File("file:///upload-neighbour/new.txt")
        self.adapter.update_file_info(neighbour)
        self.assertEqual(neighbour.attributes, {})

    def test_large_folder_has_bounded_work_and_memory(self):
        started = time.monotonic()
        for index in range(10000):
            self.adapter.update_file_info(File(f"file:///fixture/item-{index}.txt"))
        self.assertLessEqual(len(self.adapter.files), MAX_TRACKED)
        self.assertLessEqual(len(self.adapter.pending), MAX_TRACKED)
        self.assertEqual(len(self.adapter.background.submitted), MAX_TRACKED)
        self.assertLess(time.monotonic() - started, 2)

    def test_inactive_entries_free_capacity_after_green_has_expired(self):
        file = self.file()
        self.adapter.touched[file.uri] = time.monotonic() - 10
        file.callback = None  # Nautilus no longer monitors this location.
        self.adapter.drain()
        self.assertNotIn(file.uri, self.adapter.files)
