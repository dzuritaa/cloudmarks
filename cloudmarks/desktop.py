"""GI-free desktop worker. All local configuration and socket I/O happens here."""

import queue
import threading
import time

from .ipc import MAX_BATCH, UNKNOWN, load_settings, relative_uri, request

MAX_TRACKED = 512


def settings_key(settings):
    return tuple(sorted(settings.items())) if settings else None


class Background:
    def __init__(self, settings_path):
        self.settings_path = settings_path
        self.inbox = queue.Queue(MAX_TRACKED)
        self.outbox = queue.Queue(MAX_TRACKED * 2)
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self.run, name="cloudmarks-status", daemon=True)
        self.thread.start()

    def submit(self, uri):
        try:
            self.inbox.put_nowait(uri)
            return True
        except queue.Full:
            return False

    def emit(self, value):
        try:
            self.outbox.put_nowait(value)
            return True
        except queue.Full:
            # UI stopped draining: never grow memory or block helper shutdown.
            return False

    def close(self):
        self.stopped.set()

    def run(self):
        current = None
        first = True
        next_settings = retry_at = 0
        while not self.stopped.is_set():
            now = time.monotonic()
            if now >= next_settings:
                try:
                    settings = load_settings(self.settings_path)
                except Exception:
                    settings = None
                if first or settings != current:
                    if self.emit(("settings", settings)):
                        current = settings
                        first = False
                    else:
                        # Keep retrying notification after the UI drains. Until
                        # then block old-scope requests rather than reuse it.
                        current = None
                        first = True
                next_settings = now + 0.5
            try:
                batch = [self.inbox.get(timeout=0.1)]
            except queue.Empty:
                continue
            while len(batch) < MAX_BATCH:
                try:
                    batch.append(self.inbox.get_nowait())
                except queue.Empty:
                    break
            key = settings_key(current)
            mapped = {uri: relative_uri(uri, current["visible_root"]) for uri in batch} if current else {}
            mapped = {uri: path for uri, path in mapped.items() if path is not None}
            issued = time.monotonic()
            paths = list(dict.fromkeys(mapped.values()))
            if current and current["enabled"] and paths and issued >= retry_at:
                results = request(current["socket"], paths)
                if all(result == UNKNOWN for result in results.values()):
                    retry_at = time.monotonic() + 1
            else:
                results = {path: dict(UNKNOWN) for path in paths}
            for uri in batch:
                result = results.get(mapped.get(uri), dict(UNKNOWN))
                self.emit(("status", key, uri, issued + result["valid_for_ms"] / 1000, result))
