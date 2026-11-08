"""Exercise GTK callbacks and the real subprocess protocol without GI or a display."""

import io
import queue
import signal
import subprocess
import sys
import threading
import types
import unittest
from unittest.mock import Mock, patch

from cloudmarks import control_gui


class Widget:
    def __init__(self, **kwargs):
        self.label = kwargs.get("label", "")
        self.visible = True
        self.sensitive = True
        self.fraction = 0
        self.callbacks = {}
        self.closed = False

    def connect(self, name, callback):
        self.callbacks[name] = callback

    def set_label(self, text):
        self.label = text

    def set_visible(self, value):
        self.visible = value

    def set_sensitive(self, value):
        self.sensitive = value

    def set_fraction(self, value):
        self.fraction = value

    def close(self):
        self.closed = True

    def __getattr__(self, name):
        return lambda *args: None


class GuiTests(unittest.TestCase):
    def run_app(self, check, operation="keep"):
        class Application(Widget):
            def run(self, args):
                self.activate(self)
                check(self)

        gtk = types.SimpleNamespace(
            Application=Application,
            ApplicationWindow=Widget,
            Box=Widget,
            Label=Widget,
            ProgressBar=Widget,
            Button=Widget,
            Orientation=types.SimpleNamespace(VERTICAL=1),
        )
        repo = types.ModuleType("gi.repository")
        repo.Gtk = gtk
        repo.Gio = types.SimpleNamespace(ApplicationFlags=types.SimpleNamespace(NON_UNIQUE=1))
        repo.GLib = types.SimpleNamespace(timeout_add=lambda *args: None)
        gi = types.ModuleType("gi")
        gi.require_version = Mock()
        self.process = Mock(stdout=io.StringIO(""), stderr=io.StringIO(""))
        # Worker delivery is driven explicitly below to cover callback ordering.
        with (
            patch.dict(sys.modules, {"gi": gi, "gi.repository": repo}),
            patch.object(control_gui.subprocess, "Popen", return_value=self.process),
            patch.object(control_gui.threading, "Thread"),
        ):
            control_gui.main("/settings", "folder/file", operation, "a" * 64)

    def test_progress_results_success_and_close(self):
        def check(app):
            app.messages.put(("progress", {"phase": "downloading", "completed": 3, "total": 2}))
            app.messages.put(("progress", {"phase": "waiting"}))
            app.messages.put(("progress", {"phase": "error", "reason": "Close the open file"}))
            app.drain()
            self.assertEqual(app.bar.fraction, 1)
            self.assertEqual(app.label.label, "Close the open file")
            app.messages.put(("progress", {"result": True, "retained_by_other_pins": 2}))
            app.messages.put(("done", 0, ""))
            self.assertTrue(app.drain())
            self.assertIn("2 files kept", app.label.label)
            self.assertFalse(app.close_requested(app.window))
            app.cancel_or_close(app.button)
            self.assertTrue(app.window.closed)

        self.run_app(check, "free")

    def test_cancel_error_retry_restores_buttons_and_resets_progress(self):
        def check(app):
            self.process.poll.return_value = None
            self.assertTrue(app.close_requested(app.window))
            self.process.send_signal.assert_called_once_with(signal.SIGINT)
            self.assertFalse(app.button.sensitive)
            app.messages.put(("done", 130, ""))
            app.drain()
            self.assertIn("Cancelled", app.label.label)
            self.assertTrue(app.button.sensitive)
            app.start()
            self.assertEqual(app.bar.fraction, 0)
            self.assertFalse(app.finished)
            app.messages.put(("done", 1, "Disk absent; connect it"))
            app.drain()
            self.assertTrue(app.retry.visible)
            self.assertIn("connect it", app.label.label)
            app.retry.callbacks["clicked"](None)
            self.assertFalse(app.retry.visible)
            app.messages.put(("done", 0, ""))
            app.drain()
            self.assertEqual(app.label.label, "Offline selection is ready")

        self.run_app(check)

    def test_free_completion_without_other_pins_and_empty_error(self):
        def check(app):
            self.process.poll.return_value = 0
            app.cancel_or_close(app.button)
            self.process.send_signal.assert_not_called()
            app.messages.put(("progress", {"phase": "unexpected"}))
            app.messages.put(("done", 1, ""))
            app.drain()
            self.assertIn("attention", app.label.label)
            app.start()
            app.messages.put(("done", 0, ""))
            app.drain()
            self.assertIn("OneDrive files are unchanged", app.label.label)

        self.run_app(check, "free")

    def test_launch_and_command_are_isolated_and_binding_is_forwarded(self):
        with patch.object(control_gui.subprocess, "Popen") as popen:
            control_gui.launch("/settings", "file", "invalid", "a" * 64)
            popen.assert_not_called()
            control_gui.launch("/settings", "file", "keep", "a" * 64)
            self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertIn("-I", control_gui.command("/settings", "file", "keep"))
        self.assertEqual(control_gui.command("/s", "file", "keep", "a")[-2:], ["--binding", "a"])


class OutputTests(unittest.TestCase):
    def test_real_child_drains_large_stderr_and_ignores_invalid_progress(self):
        source = """
import sys,json
sys.stderr.write('diagnostic' * 20000)
for value in [[], None, {'total':-1}, {'completed':'1'}, {'phase':[]}, {'reason':None}]:
    print(json.dumps(value))
print('x' * 70000)
print('not-json')
print(json.dumps({'phase':'freeing','completed':1,'total':2}))
"""
        process = subprocess.Popen(
            [sys.executable, "-c", source], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        messages = queue.Queue(64)
        thread = threading.Thread(target=control_gui.read_output, args=(process, messages))
        thread.start()
        thread.join(5)
        self.assertFalse(thread.is_alive(), "stderr must not deadlock completion")
        self.assertEqual(messages.get_nowait()[1]["phase"], "freeing")
        done = messages.get_nowait()
        self.assertEqual(done[:2], ("done", 0))
        self.assertEqual(len(done[2]), 16384)
        self.assertTrue(messages.empty())

    def test_full_progress_queue_drops_updates_but_delivers_completion(self):
        process = Mock(stdout=io.StringIO('{"phase":"planning"}\n'), stderr=io.StringIO(""))
        process.wait.return_value = 130
        messages = Mock()
        messages.put_nowait.side_effect = queue.Full
        control_gui.read_output(process, messages)
        messages.put.assert_called_once_with(("done", 130, ""))
