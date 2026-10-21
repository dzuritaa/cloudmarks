"""GI-free desktop worker. All local configuration and socket I/O happens here."""

import json
import queue
import select
import subprocess
import sys
import threading
import time
from pathlib import Path

from .ipc import MAX_BATCH, UNKNOWN, load_settings, relative_uri, request

MAX_TRACKED = 512
MAX_WORKER_FRAME = 65536


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


class ProcessBackground(Background):
    """Keep filesystem/socket work outside Nautilus's embedded Python runtime.

    The embedding can delay Python threads between their individual system
    calls, consuming a short evidence lease even when the helper is responsive.
    Only bounded pipe messages cross the bridge thread; UI callbacks still use
    in-memory queues, and the original evidence deadlines remain unchanged.
    """

    def run(self):
        python = sys.executable if Path(sys.executable).name.startswith("python") else "/usr/bin/python3"
        process = None
        try:
            process = subprocess.Popen(
                [
                    python,
                    "-I",
                    "-u",
                    "-c",
                    "import sys; sys.path.insert(0, sys.argv[1]); "
                    "from cloudmarks.desktop import pipe_worker; pipe_worker(sys.argv[2])",
                    str(Path(__file__).resolve().parent.parent),
                    str(self.settings_path),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
            incoming = bytearray()
            while not self.stopped.is_set() and process.poll() is None:
                batch = []
                for _ in range(MAX_BATCH):
                    try:
                        batch.append(self.inbox.get_nowait())
                    except queue.Empty:
                        break
                if batch:
                    # Bound each message before sending it to the child.
                    for uri in batch:
                        frame = (json.dumps(uri) + "\n").encode()
                        if len(frame) <= MAX_WORKER_FRAME:
                            remaining = memoryview(frame)
                            while remaining:
                                written = process.stdin.write(remaining)
                                if not written:
                                    raise OSError("Worker pipe stopped accepting requests")
                                remaining = remaining[written:]
                if select.select([process.stdout], [], [], 0.05)[0]:
                    block = process.stdout.read(4096)
                    if not block:
                        break
                    incoming.extend(block)
                    while b"\n" in incoming:
                        line, _, rest = incoming.partition(b"\n")
                        incoming = bytearray(rest)
                        if len(line) > MAX_WORKER_FRAME:
                            raise ValueError("Worker frame exceeded its bound")
                        event = json.loads(line)
                        if event[0] == "status" and event[1] is not None:
                            event[1] = tuple(tuple(item) for item in event[1])
                        self.emit(tuple(event))
                    if len(incoming) > MAX_WORKER_FRAME:
                        raise ValueError("Worker frame exceeded its bound")
        except (OSError, ValueError, TypeError, IndexError):
            pass  # Losing the worker clears settings and all positive evidence.
        finally:
            self.emit(("settings", None))
            if process:
                process.stdin.close()
                process.stdout.close()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()


def pipe_worker(settings_path):
    """Private child entry point. EOF stops this worker, never the status helper."""

    class PipeBackground(Background):
        def emit(self, value):
            try:
                frame = json.dumps(value) + "\n"
                if len(frame.encode()) > MAX_WORKER_FRAME:
                    return False
                sys.stdout.write(frame)
                sys.stdout.flush()
                return True
            except (OSError, ValueError):
                self.close()
                return False

    worker = PipeBackground(settings_path)
    try:
        while line := sys.stdin.readline(MAX_WORKER_FRAME + 1):
            if len(line.encode()) > MAX_WORKER_FRAME or not line.endswith("\n"):
                break
            uri = json.loads(line)
            if not isinstance(uri, str) or len(uri) > 16384:
                break
            worker.submit(uri)
    finally:
        worker.close()
        worker.thread.join(timeout=2)
