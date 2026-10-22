"""Read-only Nautilus presentation; callbacks do no disk, socket or cloud I/O.

COMPLETE returns immediately with a cached result or unknown. A background
worker delivers evidence; invalidation asks Nautilus to repaint asynchronously.
This avoids unsafe closure/NULL-handle reuse in the current Python binding.
"""

import os
import queue
import time
from pathlib import Path

import gi

gi.require_version("Nautilus", "4.1")
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, GObject, Gtk, Nautilus  # noqa: E402

from .desktop import MAX_TRACKED, ProcessBackground, settings_key  # noqa: E402
from .ipc import UNKNOWN, mapped_uri  # noqa: E402

Background = ProcessBackground

PRESENTATION = {
    "cloud_only": ("cloudmarks-cloud", "Cloud only"),
    "saved_locally": ("cloudmarks-local", "Saved locally"),
    "kept_offline": ("cloudmarks-pin", "Kept offline"),
    "transferring": ("cloudmarks-transfer", "Downloading"),
    "uploading": ("cloudmarks-transfer", "Uploading"),
    "error": ("cloudmarks-error", "Needs attention"),
    "unknown": ("cloudmarks-unknown", "Needs checking"),
}


class CloudMarksExtension(GObject.GObject, Nautilus.InfoProvider, Nautilus.ColumnProvider):
    def __init__(self):
        super().__init__()
        config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        self.background = Background(config_home / "cloudmarks/desktop.json")
        self.settings = None
        self.files = {}
        self.cache = {}
        self.pending = {}
        self.touched = {}
        # Bundled outlined symbols have explicit light/dark contrast. Register a
        # process-local search path; do not change the user's selected icon theme.
        display = Gdk.Display.get_default()
        self.theme = Gtk.IconTheme.get_for_display(display) if display else None
        if self.theme:
            self.theme.add_search_path(str(Path(__file__).parent / "icons"))
        self.available_icons = {
            icon for icon, _ in PRESENTATION.values() if self.theme and self.theme.has_icon(icon)
        }
        self.fallback_icon = (
            "emblem-important" if self.theme and self.theme.has_icon("emblem-important") else None
        )
        self.timer = GLib.timeout_add(100, self.drain)

    def get_columns(self):
        return [
            Nautilus.Column(
                name="CloudMarks::status",
                attribute="cloudmarks_status",
                label="CloudMarks",
                description="Read-only cloud status and evidence; actions use the separate CLI",
            )
        ]

    def scoped(self, uri):
        return bool(self.settings and self.settings["enabled"] and mapped_uri(uri, self.settings) is not None)

    def update_file_info(self, file):
        uri = file.get_uri()
        # Retain a bounded set while asynchronous settings load. Never resolve
        # URIs or traverse directories here, including for unrelated locations.
        if self.settings is None:
            if len(self.files) < MAX_TRACKED:
                self.files[uri] = file
            return
        if not self.scoped(uri):
            return
        if uri in self.files or len(self.files) < MAX_TRACKED:
            self.files[uri] = file
        now = time.monotonic()
        if uri in self.files:
            self.touched[uri] = now
        cached = self.cache.get(uri)
        result = cached[1] if cached and cached[0] > now else UNKNOWN
        icon, label = PRESENTATION[result["state"]]
        if icon in self.available_icons:
            file.add_emblem(icon)
        elif self.fallback_icon:
            file.add_emblem(self.fallback_icon)
        view, _ = mapped_uri(uri, self.settings)
        scope = (
            "fixture"
            if self.settings["mode"] == "fixture"
            else ("test account, uploads" if view == "upload" else "test account, read-only")
        )
        file.add_string_attribute("cloudmarks_status", f"{label} ({scope}) — {result['reason']}")
        if uri in self.files and not cached and uri not in self.pending:
            if self.background.submit(uri):
                self.pending[uri] = now

    def drain(self):
        now = time.monotonic()
        changed = set()
        for _ in range(MAX_TRACKED * 2):
            try:
                event = self.background.outbox.get_nowait()
            except queue.Empty:
                break
            if event[0] == "settings":
                old_files = list(self.files.items())
                self.settings = event[1]
                self.cache.clear()
                self.pending.clear()
                self.files = {uri: file for uri, file in old_files if self.scoped(uri)}
                # Clearing settings also clears existing marks, including when
                # disabled, missing or malformed. No mount control is involved.
                for _, file in old_files:
                    file.invalidate_extension_info()
            else:
                _, key, uri, expires, result = event
                if key != settings_key(self.settings) or uri not in self.files:
                    continue
                self.pending.pop(uri, None)
                if self.scoped(uri):
                    # A response older than its lease must still schedule retry;
                    # it can never restore green after a slow/hung helper.
                    previous = self.cache.get(uri)
                    fresh = (expires, result) if expires > now else (now + 0.5, UNKNOWN)
                    self.cache[uri] = fresh
                    if expires <= now:
                        self.background.note("response_late", late_ms=(now - expires) * 1000)
                    if not previous or previous[1]["state"] != fresh[1]["state"]:
                        self.background.note(
                            "status_changed",
                            state=fresh[1]["state"],
                            previous_state=previous[1]["state"] if previous else "none",
                        )
                    # Renew unchanged evidence silently. Nautilus clears extension
                    # attributes during invalidation, so repaint only changes or
                    # an occasional interest probe for releasing old locations.
                    if (
                        not previous
                        or (previous[1]["state"], previous[1]["reason"])
                        != (fresh[1]["state"], fresh[1]["reason"])
                        or now - self.touched.get(uri, now) > 4
                    ):
                        changed.add(uri)
        for uri, (expires, cached_result) in list(self.cache.items()):
            if expires <= now:
                self.background.note(
                    "lease_expired", state=cached_result["state"], late_ms=(now - expires) * 1000
                )
                del self.cache[uri]
                changed.add(uri)
            elif (
                expires - now <= (0.15 if cached_result["state"] == "unknown" else 0.7)
                and now - self.touched.get(uri, now) < 5
                and uri not in self.pending
            ):
                # Allow the 600 ms socket deadline plus a GLib tick before expiry.
                # If the worker is
                # slow, the existing lease still expires on schedule.
                if self.background.submit(uri):
                    self.pending[uri] = now
        for uri, submitted in list(self.pending.items()):
            if now - submitted > 2:
                self.background.note("pending_timeout", elapsed_ms=(now - submitted) * 1000)
                self.pending.pop(uri)
                changed.add(uri)
        for uri in changed:
            file = self.files.get(uri)
            if file:
                file.invalidate_extension_info()
        # Drop vanished files; no directory walk, metadata query or open occurs.
        for uri, file in list(self.files.items()):
            if file.is_gone() or (now - self.touched.get(uri, now) > 6 and uri not in self.cache):
                self.files.pop(uri)
                self.cache.pop(uri, None)
                self.pending.pop(uri, None)
                self.touched.pop(uri, None)
        return GLib.SOURCE_CONTINUE
