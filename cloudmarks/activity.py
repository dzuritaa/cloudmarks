"""Opt-in, bounded observation of real uploads and cloud metadata.

Two background workers keep network latency and VFS-cache work out of the
status socket. They never pin, download, publish, evict or modify the state DB.
Only rclone's read-only queue/stats endpoints are called, over a private socket.
"""

import http.client
import json
import os
import socket
import stat
import threading
import time

from .ipc import LEASE_MS, private_directory, same_user
from .model import ConfigurationError, ScopeError, StorageError, canonical_drive
from .offline_config import mounted_view
from .onedrive import OneDrive, private_config
from .paths import READ_FILE, fingerprint, open_absolute_directory, relative_parts
from .telemetry import error_category, record

LIMIT = 128
SCAN_SECONDS = 10
FRESH_SECONDS = 30
LOCAL_SECONDS = 3
MAX_RESPONSE = 256 * 1024


class UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=1)
        self.path = path

    def connect(self):
        from pathlib import Path

        path = Path(self.path)
        fd = private_directory(path.parent)
        try:
            info = os.stat(path.name, dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise ConfigurationError("Upload socket must belong to the current user")
        finally:
            os.close(fd)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)
        if not same_user(self.sock):
            raise ConfigurationError("Upload socket peer belongs to another user")


def rc(path, method):
    if method not in ("vfs/queue", "vfs/stats"):
        raise ScopeError("Observer accepts only read-only VFS methods")
    connection = UnixHTTP(path)
    try:
        connection.request(
            "POST", "/" + method, json.dumps({"fs": "upload:"}), {"Content-Type": "application/json"}
        )
        response = connection.getresponse()
        payload = response.read(MAX_RESPONSE + 1)
        if response.status != 200 or len(payload) > MAX_RESPONSE:
            raise StorageError("Upload queue is unavailable or exceeds the observer limit")
        value = json.loads(payload)
        if not isinstance(value, dict):
            raise StorageError("Invalid upload queue response")
        return value
    finally:
        connection.close()


def cache_entries(data_path, meta_path):
    """Bounded metadata/stat inspection only; never read or hydrate file content."""
    root = open_absolute_directory(data_path)
    meta = open_absolute_directory(meta_path)
    values, count = {}, 0
    try:
        # fwalk pins directory descriptors and does not follow symbolic links.
        for directory, dirs, files, fd in os.fwalk(".", dir_fd=root, follow_symlinks=False):
            depth = len(directory.split("/")) - 1
            count += len(dirs) + len(files)
            if depth > 16 or count > LIMIT:
                raise StorageError("Upload cache exceeds the observation limit")
            for name in dirs:
                if stat.S_ISLNK(os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode):
                    raise ScopeError("Upload cache contains a directory link")
            for name in files:
                path = (directory[2:] + "/" if directory != "." else "") + name
                relative_parts(path)
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ScopeError("Upload cache contains an unsupported file")
                # Walk metadata components with O_NOFOLLOW as well.
                parent = os.dup(meta)
                try:
                    for part in relative_parts(path)[:-1]:
                        child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                        os.close(parent)
                        parent = child
                    content = os.open(name, READ_FILE, dir_fd=parent)
                    try:
                        meta_info = os.fstat(content)
                        if not stat.S_ISREG(meta_info.st_mode) or meta_info.st_nlink != 1:
                            raise ScopeError("Upload metadata is not a regular file")
                        data = os.read(content, 16385)
                        if len(data) > 16384:
                            raise StorageError("Upload metadata exceeds its limit")
                        value = json.loads(data)
                        if not isinstance(value, dict) or type(value.get("Dirty")) is not bool:
                            raise StorageError("Upload metadata is incomplete")
                        dirty = value["Dirty"]
                    finally:
                        os.close(content)
                except (FileNotFoundError, json.JSONDecodeError):
                    # New/open files can precede their metadata. Never confirm
                    # cloud completion while that local write is unaccounted for.
                    dirty = True
                finally:
                    os.close(parent)
                values[path] = (fingerprint(info), dirty)
        return values
    finally:
        os.close(root)
        os.close(meta)


class Activity:
    def __init__(self, config, offline, settings, *, backend=None, clock=time.time):
        self.config, self.offline, self.settings, self.clock = config, offline, settings, clock
        self.backend = backend or OneDrive(config, offline)
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.remote = {}
        self.local = {}
        self.after = {}
        self.local_at = 0
        self.threads = []
        self.validate_upload()

    def validate_upload(self):
        parser = private_config(self.settings["upload_config"])
        if set(parser.sections()) != {"upload"}:
            raise ConfigurationError("Expected a dedicated upload configuration")
        upload = parser["upload"]
        if (
            upload.get("type") != "onedrive"
            or canonical_drive(upload.get("drive_id")) != self.config.expected_drive_id
            or upload.get("root_folder_id")
            != self.config.expected_drive_id + "#" + self.config.allowed_root_id
        ):
            raise ScopeError("Upload configuration does not match the verified cloud folder")

    def start(self):
        for name, target in (
            ("cloudmarks-upload-queue", self.local_worker),
            ("cloudmarks-cloud-observer", self.remote_worker),
        ):
            thread = threading.Thread(name=name, target=target, daemon=True)
            self.threads.append(thread)
            thread.start()
        return self

    def close(self):
        self.stop.set()
        self.wake.set()
        for thread in self.threads:
            thread.join(timeout=0.5)

    def update_local(self, entries, queue):
        if not isinstance(queue, list) or len(queue) > LIMIT:
            raise StorageError("Upload queue exceeds the observation limit")
        states = {}
        for item in queue:
            path = item["name"]
            relative_parts(path)
            if type(item.get("tries")) is not int or item["tries"] < 0:
                raise StorageError("Invalid upload retry count")
            states[path] = "error" if item["tries"] > 0 and not item.get("uploading") else "uploading"
        for path, (_, dirty) in entries.items():
            if dirty:
                states.setdefault(path, "uploading")
        if len(set(entries) | set(states)) > LIMIT:
            raise StorageError("Upload activity exceeds the observation limit")
        now = self.clock()
        with self.lock:
            old_entries = getattr(self, "entries", {})
            old_states = self.local
            # Removed/renamed cache entries need a confirmation wakeup too;
            # otherwise deletion waits for the next periodic cloud scan.
            for path in set(entries) | set(states) | set(old_entries) | set(old_states):
                if entries.get(path) != old_entries.get(path) or states.get(path) != old_states.get(path):
                    self.after[path] = now
                    self.wake.set()
            # Cache eviction must not expose metadata older than the last write.
            self.after = {
                path: stamp
                for path, stamp in self.after.items()
                if path in entries or path in states or now - stamp < FRESH_SECONDS + SCAN_SECONDS
            }
            if len(self.after) > LIMIT:
                raise StorageError("Recent upload activity exceeds the observation limit")
            self.entries, self.local, self.local_at = entries, states, now

    def local_worker(self):
        while not self.stop.is_set():
            try:
                self.validate_upload()
                mount = mounted_view({"visible_root": self.settings["upload_root"]})
                if not mount or mount["type"] != "fuse.rclone" or "rw" not in mount["options"]:
                    raise StorageError("Upload mount is unavailable")
                stats = rc(self.settings["upload_socket"], "vfs/stats")["diskCache"]
                queue = rc(self.settings["upload_socket"], "vfs/queue").get("queue", [])
                entries = cache_entries(stats["path"], stats["pathMeta"])
                self.update_local(entries, queue)
            except Exception as error:
                # A transient timeout does not revoke a still-live observation.
                # Repeated failures expire naturally at the original deadline.
                record("activity", "queue_failed", error=error_category(error))
            self.stop.wait(0.5)

    def update_remote(self, observations):
        if len(observations) > LIMIT:
            raise StorageError("Cloud catalog exceeds the observation limit")
        values, identities, keys = {}, set(), set()
        for observation in observations:
            remote = observation.remote
            self.config.validate_remote(remote)
            key = self.config.path_key(remote.relative_path)
            if remote.identity in identities or key in keys or remote.deleted:
                raise ScopeError("Cloud catalog has duplicate or unavailable items")
            identities.add(remote.identity)
            keys.add(key)
            values[remote.relative_path] = remote
        with self.lock:
            # An omitted item becomes unknown, never a deletion or eviction.
            self.remote = values
        record("activity", "scan_completed", count=len(values))

    def remote_worker(self):
        backoff = 5
        while not self.stop.is_set():
            self.wake.clear()
            started = time.monotonic()
            try:
                self.update_remote(self.backend.catalog(cancelled=self.stop.is_set))
                delay, backoff = SCAN_SECONDS, 5
            except Exception as error:
                # Keep old observations only until their own deadline. A failed
                # request must never renew metadata or offline evidence.
                record("activity", "scan_failed", error=error_category(error))
                delay, backoff = backoff, min(60, backoff * 2)
            # Do not let a large upload generate unbounded Graph polling.
            if self.stop.wait(max(0, 3 - (time.monotonic() - started))):
                break
            self.wake.wait(max(0, delay - (time.monotonic() - started)))

    def evidence(self, path):
        """Memory-only status evidence; caller still proves durable offline bytes."""
        now = self.clock()
        with self.lock:
            remote = self.remote.get(path)
            local = self.local.get(path)
            after = self.after.get(path, 0)
            entry = getattr(self, "entries", {}).get(path)
            local_age = now - self.local_at
            local_fresh = 0 <= local_age < LOCAL_SECONDS
            local_remaining = LOCAL_SECONDS - local_age
        if not local_fresh:
            return None, self.result("unknown", "Upload observer unavailable; completion needs checking")
        if local:
            return None, self.result(
                local,
                "Upload failed; rclone will retry"
                if local == "error"
                else "Local write is queued or uploading to OneDrive",
                remaining=local_remaining,
            )
        if (
            remote is None
            or not 0 <= now - remote.observed_at < min(FRESH_SECONDS, self.config.max_age_seconds)
            or remote.observed_at <= after
            or entry is not None
            and remote.size != entry[0][2]
        ):
            return None, self.result("unknown", "Waiting for fresh OneDrive confirmation")
        remaining = min(FRESH_SECONDS, self.config.max_age_seconds) - (now - remote.observed_at)
        return remote, self.result(
            "cloud_only",
            "Confirmed in OneDrive; upload cache is not an offline copy",
            remaining=min(remaining, local_remaining),
        )

    @staticmethod
    def result(state, reason, *, remaining=1):
        return {
            "state": state,
            "reason": reason,
            "valid_for_ms": max(1, min(LEASE_MS, int(remaining * 1000))),
        }


class ObservedStore:
    """Read-only metadata overlay. Durable pin/copy/issue state stays authoritative."""

    def __init__(self, store, activity):
        self.store, self.activity = store, activity

    def __getattr__(self, name):
        return getattr(self.store, name)

    def remote(self, identity):
        previous = self.store.remote(identity)
        if previous is not None:
            current, _ = self.activity.evidence(previous.relative_path)
            if current is not None and current.identity == identity:
                return current
        return previous
