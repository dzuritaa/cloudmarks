"""Bounded desktop bridge parsing without launching a desktop or subprocess."""

import io
import json
import threading
import unittest
from unittest.mock import Mock, patch

from cloudmarks import desktop
from cloudmarks.model import ScopeError


class PipeWorkerTests(unittest.TestCase):
    def run_pipe(self, frames, *, stdout=None):
        output = stdout or io.StringIO()
        with (
            patch.object(desktop.threading, "Thread") as thread,
            patch.object(desktop.sys, "stdin", io.StringIO(frames)),
            patch.object(desktop.sys, "stdout", output),
            patch("cloudmarks.control_gui.launch") as launch,
            patch.object(desktop, "record") as record,
        ):
            desktop.pipe_worker("/fake/settings")
            worker = thread.call_args.kwargs["target"].__self__
        return worker, launch, record, output

    def test_valid_status_controls_diagnostics_and_eof_stop_cleanly(self):
        values = [
            "file:///view/a",
            {"diagnostic": "status_changed", "fields": {"state": "unknown"}},
            {"control": "keep", "path": "folder/file", "binding": "a" * 64},
        ]
        worker, launch, record, output = self.run_pipe("".join(json.dumps(v) + "\n" for v in values))
        self.assertEqual(worker.inbox.get_nowait(), "file:///view/a")
        launch.assert_called_once_with("/fake/settings", "folder/file", "keep", "a" * 64)
        record.assert_called_once_with("desktop", "status_changed", state="unknown")
        self.assertTrue(worker.stopped.is_set())
        with patch.object(desktop.sys, "stdout", output):
            self.assertTrue(worker.emit(("settings", None)))
        self.assertEqual(json.loads(output.getvalue())[0], "settings")
        self.assertFalse(worker.emit(("status", "x" * 70000)))

    def test_invalid_or_unterminated_frames_stop_without_launching(self):
        for value in [
            123,
            {},
            {"diagnostic": 1, "fields": {}},
            {"diagnostic": "x", "fields": []},
            {"control": "delete", "path": "folder", "binding": "a" * 64},
            {"control": "keep", "path": "folder", "binding": "bad"},
        ]:
            worker, launch, _, _ = self.run_pipe(json.dumps(value) + "\n")
            self.assertTrue(worker.stopped.is_set())
            launch.assert_not_called()
        for frame in ('"unterminated"', "x" * 65537):
            worker, launch, _, _ = self.run_pipe(frame)
            launch.assert_not_called()
        with self.assertRaises(ScopeError):
            self.run_pipe(json.dumps({"control": "keep", "path": "../escape", "binding": "a" * 64}) + "\n")

    def test_broken_output_stops_worker_and_queue_pressure_is_bounded(self):
        worker, _, _, _ = self.run_pipe("")
        with patch.object(desktop.sys, "stdout", Mock(write=Mock(side_effect=BrokenPipeError))):
            self.assertFalse(worker.emit(("settings", None)))
        with patch.object(desktop.threading, "Thread"):
            bounded = desktop.Background("/settings")
        for _ in range(desktop.MAX_TRACKED):
            self.assertTrue(bounded.submit("uri"))
        self.assertFalse(bounded.submit("overflow"))
        for _ in range(desktop.MAX_TRACKED * 2):
            self.assertTrue(bounded.emit(("status",)))
        self.assertFalse(bounded.emit(("status",)))
        for _ in range(64):
            self.assertTrue(bounded.note("status_changed"))
        self.assertFalse(bounded.note("overflow"))
        self.assertFalse(bounded.control("file", "keep", "a" * 64))
        self.assertFalse(bounded.control("file", "invalid", "a" * 64))
        bounded.notes.get_nowait()
        self.assertTrue(bounded.control("file", "free", "a" * 64))
        bounded.close()

    def test_healthy_restart_resets_backoff_and_close_interrupts_wait(self):
        worker = desktop.ProcessBackground.__new__(desktop.ProcessBackground)
        worker.stopped = threading.Event()
        attempts = []

        def once():
            attempts.append(1)
            if len(attempts) == 2:
                worker.stopped.set()

        worker.run_once = once
        with (
            patch.object(desktop.time, "monotonic", side_effect=[0, 100, 101]),
            patch.object(worker.stopped, "wait", return_value=False) as wait,
        ):
            worker.run()
        wait.assert_called_once_with(1)
        self.assertEqual(len(attempts), 2)
