"""Small external GTK progress/retry window, never imported by Nautilus."""

import json
import queue
import signal
import subprocess
import threading
from pathlib import Path


def command(settings_path, path, operation, binding=None):
    return [
        "/usr/bin/python3",
        "-I",
        "-u",
        "-c",
        "import sys;sys.path.insert(0,sys.argv.pop(1));from cloudmarks.__main__ import main;main()",
        str(Path(__file__).resolve().parent.parent),
        "control",
        "--settings",
        str(settings_path),
        "--operation",
        operation,
        "--path",
        path,
    ] + (["--binding", binding] if binding else [])


def launch(settings_path, path, operation, binding):
    if operation not in ("keep", "free"):
        return
    subprocess.Popen(
        [
            "/usr/bin/python3",
            "-I",
            "-c",
            "import sys;sys.path.insert(0,sys.argv[1]);from cloudmarks.control_gui import main;main(*sys.argv[2:])",
            str(Path(__file__).resolve().parent.parent),
            str(settings_path),
            path,
            operation,
            binding,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def main(settings_path, path, operation, binding):
    import gi

    gi.require_version("Gtk", "4.0")
    from gi.repository import Gio, GLib, Gtk

    phases = {
        "planning": "Checking the selection…",
        "waiting": "Preparing offline files…",
        "downloading": "Saving offline copies…",
        "freeing": "Freeing local copies…",
        "restoring": "Restoring the read-only view…",
        "verifying": "Verifying offline copies…",
        "complete": "Complete",
        "cancelled": "Cancelled — previous files are preserved",
        "error": "The operation needs attention",
    }

    class App(Gtk.Application):
        def __init__(self):
            super().__init__(
                application_id="io.github.dzuritaa.CloudMarks.Controls", flags=Gio.ApplicationFlags.NON_UNIQUE
            )
            self.messages = queue.Queue(maxsize=64)
            self.process = None
            self.finished = False
            self.connect("activate", self.activate)

        def activate(self, application):
            self.window = Gtk.ApplicationWindow(application=self, title="CloudMarks")
            self.window.set_default_size(480, 240)
            self.window.connect("close-request", self.close_requested)
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
            for side in ("top", "bottom", "start", "end"):
                getattr(box, "set_margin_" + side)(20)
            self.window.set_child(box)
            title = Gtk.Label(label="Keep offline" if operation == "keep" else "Free local copy", xalign=0)
            title.add_css_class("title-2")
            box.append(title)
            selected = Gtk.Label(label=path, xalign=0, wrap=True, selectable=True)
            box.append(selected)
            self.label = Gtk.Label(label=phases["planning"], xalign=0, wrap=True)
            box.append(self.label)
            self.bar = Gtk.ProgressBar(show_text=True)
            box.append(self.bar)
            self.retry = Gtk.Button(label="Retry")
            self.retry.set_visible(False)
            self.retry.connect("clicked", lambda unused: self.start())
            box.append(self.retry)
            self.button = Gtk.Button(label="Cancel")
            self.button.connect("clicked", self.cancel_or_close)
            box.append(self.button)
            self.window.present()
            GLib.timeout_add(100, self.drain)
            self.start()

        def start(self):
            self.finished = False
            self.retained = 0
            self.retry.set_visible(False)
            self.button.set_label("Cancel")
            self.label.set_label(phases["planning"])
            self.bar.set_fraction(0)
            self.process = subprocess.Popen(
                command(settings_path, path, operation, binding),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )

            def read():
                errors = []

                def error_reader():
                    errors.append(self.process.stderr.read(16384))

                err_thread = threading.Thread(target=error_reader, daemon=True)
                err_thread.start()
                for line in self.process.stdout:
                    if len(line) > 65536:
                        continue
                    try:
                        value = json.loads(line)
                    except ValueError:
                        continue
                    try:
                        self.messages.put_nowait(("progress", value))
                    except queue.Full:
                        pass
                code = self.process.wait()
                err_thread.join(timeout=1)
                self.process.stdout.close()
                self.process.stderr.close()
                self.messages.put(("done", code, "".join(errors)))

            threading.Thread(target=read, daemon=True).start()

        def drain(self):
            for _ in range(64):
                try:
                    item = self.messages.get_nowait()
                except queue.Empty:
                    break
                if item[0] == "progress":
                    value = item[1]
                    if value.get("result"):
                        self.retained = value.get("retained_by_other_pins", 0)
                        continue
                    phase = value.get("phase", "planning")
                    label = value.get("reason") if phase == "error" else phases.get(phase, phases["planning"])
                    total, completed = value.get("total", 0), value.get("completed", 0)
                    self.label.set_label(label or phases["error"])
                    if total:
                        self.bar.set_fraction(min(1, completed / total))
                        self.bar.set_text(f"{completed} / {total} files")
                    else:
                        self.bar.pulse()
                else:
                    self.finished = True
                    self.button.set_label("Close")
                    self.button.set_sensitive(True)
                    if item[1] == 0:
                        self.bar.set_fraction(1)
                        self.label.set_label(
                            "Offline selection is ready"
                            if operation == "keep"
                            else (
                                f"Local copies released; {self.retained} files kept by other offline selections"
                                if self.retained
                                else "Local copies released; OneDrive files are unchanged"
                            )
                        )
                    elif item[1] == 130:
                        self.label.set_label(phases["cancelled"])
                    else:
                        self.label.set_label(item[2].strip() or phases["error"])
                        self.retry.set_visible(True)
            return True

        def cancel_or_close(self, button):
            if self.finished:
                self.window.close()
            elif self.process and self.process.poll() is None:
                self.process.send_signal(signal.SIGINT)
                self.label.set_label("Cancelling safely and restoring the view…")
                self.button.set_sensitive(False)

        def close_requested(self, window):
            if not self.finished:
                self.cancel_or_close(self.button)
                return True
            return False

    App().run([])
