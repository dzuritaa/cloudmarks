"""Disposable M0.1 fixture-only extension. Do not import this into production.

Loaded only in the isolated desktop made by run_isolated.py. The deliberately
hard-coded labels are presentation fixtures, never evidence about real files.
"""

import json
import os
import time
from pathlib import Path

import gi

gi.require_version("Nautilus", "4.1")
gi.require_version("Gtk", "4.0")
from gi.repository import GLib, GObject, Gtk, Nautilus  # noqa: E402 - select GI versions before importing

STATES = {
    "01-cloud.txt": ("emblem-downloads", "Cloud only (fixture)"),
    "02-local.txt": ("emblem-default", "Saved locally (fixture)"),
    "03-transfer.txt": ("emblem-synchronizing", "Downloading (fixture)"),
    "04-error.txt": ("emblem-important", "Error (fixture)"),
    "05-unknown.txt": ("emblem-dialog-question", "Needs checking (fixture)"),
    "06-refresh.txt": ("emblem-downloads", "Before refresh (fixture)"),
}


class CloudMarksBadgeSpike(GObject.GObject, Nautilus.InfoProvider, Nautilus.ColumnProvider):
    def __init__(self):
        super().__init__()
        # Xvfb has no settings daemon. Set the sandbox process's icon theme
        # explicitly so this measures the installed desktop theme, not fallback.
        Gtk.Settings.get_default().set_property("gtk-icon-theme-name", "Yaru")
        self.root = Path(os.environ["CLOUDMARKS_SPIKE_ROOT"]).as_uri() + "/"
        self.pending = {}
        self.counter = 0
        self.refreshed = False
        self.started = time.monotonic()
        self.log("loaded")

    def log(self, event, **details):
        # Fixture basenames and timing only; no personal paths or listings.
        with open(os.environ["CLOUDMARKS_SPIKE_LOG"], "a", encoding="utf-8") as stream:
            stream.write(
                json.dumps({"event": event, "seconds": round(time.monotonic() - self.started, 4), **details})
                + "\n"
            )

    def get_columns(self):
        return [
            Nautilus.Column(
                name="CloudMarksSpike::status",
                attribute="cloudmarks_spike_status",
                label="CloudMarks fixture",
                description="Hard-coded M0.1 status; not real cloud state",
            )
        ]

    def update_file_info_full(self, provider, handle, closure, file):
        # Exact fixture URI match; no filesystem work or remote requests here.
        uri = file.get_uri()
        name = file.get_name()
        if name not in STATES or uri != self.root + name:
            return Nautilus.OperationResult.COMPLETE
        self.counter += 1
        request = self.counter
        source = GLib.timeout_add(40, self.complete, request, provider, handle, closure, file)
        self.pending[request] = (source, handle)
        self.log("scheduled", name=name, request=request)
        return Nautilus.OperationResult.IN_PROGRESS

    def complete(self, request, provider, handle, closure, file):
        self.pending.pop(request, None)
        name = file.get_name()
        emblem, label = STATES[name]
        if name == "06-refresh.txt" and self.refreshed:
            emblem, label = "emblem-default", "After refresh (fixture)"
        file.add_emblem(emblem)
        file.add_string_attribute("cloudmarks_spike_status", label)
        self.log("completed", name=name, request=request, emblem=emblem, label=label)
        Nautilus.info_provider_update_complete_invoke(
            closure, provider, handle, Nautilus.OperationResult.COMPLETE
        )
        if name == "06-refresh.txt" and not self.refreshed:
            self.refreshed = True
            GLib.timeout_add(1500, self.refresh, file)
        return GLib.SOURCE_REMOVE

    def refresh(self, file):
        self.log("invalidated", name=file.get_name())
        file.invalidate_extension_info()
        return GLib.SOURCE_REMOVE

    def cancel_update(self, provider, handle):
        # The binding may represent handles as NULL. Remove all matching
        # timers rather than leaving a callback to a cancelled closure.
        for request, (source, pending_handle) in list(self.pending.items()):
            if pending_handle == handle:
                GLib.source_remove(source)
                self.pending.pop(request)
                self.log("cancelled", request=request)
