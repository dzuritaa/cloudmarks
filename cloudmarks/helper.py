"""Single-owner local status service. IPC exposes no transfer or state writes.

The socket protocol is status-only. In account mode (settings v3/v4) the same
process also runs the upload Activity and FolderObserver workers, which hold
read-only OneDrive transport access for metadata; the socket thread itself
never constructs transport or reads credentials.
"""

import fcntl
import json
import os
import selectors
import socket
import sqlite3
import stat
import time
from pathlib import Path
from typing import TYPE_CHECKING

from .config import load_config
from .engine import CHECKING_REASON, StatusEngine
from .ipc import LEASE_MS, MAX_BATCH, MAX_FRAME, TIMEOUT, VERSION, encode, private_directory, same_user
from .model import CloudMarksError, ConfigurationError, FileIdentity
from .paths import SecureRoot, relative_parts
from .state import StateStore
from .telemetry import enabled, error_category, record

if TYPE_CHECKING:
    from .activity import Activity, ObservedStore
    from .config import Config
    from .state import ReadStore

MAX_VERIFY_BYTES = 8 * 1024 * 1024
UNAVAILABLE_MS = 500
# Ask again soon while a background content check is running.
CHECKING_MS = 250
# Errors from state, disk or configuration that make one path's evidence
# temporarily unavailable. They must degrade that path, never stop the helper.
PATH_ERRORS = (sqlite3.Error, OSError, CloudMarksError)


def unavailable():
    return {
        "state": "unknown",
        "reason": "Status evidence is temporarily unavailable",
        "valid_for_ms": UNAVAILABLE_MS,
    }


class BaseStatusService:
    """Shared read-only status protocol. Subclasses provide store, config and engine."""

    supports_large_files = False
    activity: Activity | None = None
    store: StateStore | ReadStore | ObservedStore
    config: Config
    engine: StatusEngine

    def lookup(self, path):
        relative_parts(path)
        row = self.store.file_at(path)
        result = {
            "state": "unknown",
            "reason": "No verified observation for this path",
            "valid_for_ms": LEASE_MS,
        }
        if row is None:
            return result
        identity = FileIdentity(self.config.expected_drive_id, row[0])
        remote = self.store.remote(identity)
        if remote.relative_path != path:
            return result
        if remote.size > MAX_VERIFY_BYTES and not self.supports_large_files:
            result["reason"] = "Prototype verification size limit; needs checking"
            return result
        status = self.engine.status(identity)
        remaining = self.config.max_age_seconds - (time.time() - remote.observed_at)
        if status.reason == CHECKING_REASON:
            lease = CHECKING_MS
        elif status.state.value == "unknown":
            lease = LEASE_MS
        else:
            lease = max(1, min(LEASE_MS, int(remaining * 1000)))
        result.update(state=status.state.value, reason=status.reason, valid_for_ms=lease)
        return result

    def lookup_view(self, path, view):
        return self.lookup(path)

    def handle(self, value):
        if (
            not isinstance(value, dict)
            or set(value) not in ({"version", "op", "paths"}, {"version", "op", "paths", "view"})
            or type(value["version"]) is not int
            or value["version"] != VERSION
            or value["op"] != "status"
            or not isinstance(value["paths"], list)
            or not 1 <= len(value["paths"]) <= MAX_BATCH
        ):
            raise ValueError("Invalid read-only status request")
        view = value.get("view", "offline")
        if view not in ("offline", "upload") or (view == "upload" and not self.activity):
            raise ValueError("Unsupported status view")
        paths = value["paths"]
        for path in paths:
            relative_parts(path)
        if len(set(paths)) != len(paths):
            raise ValueError("Duplicate request paths")
        deadline = time.monotonic() + 0.15
        results = {}
        failures: dict[str, int] = {}
        for path in paths:
            if time.monotonic() >= deadline:
                results[path] = {
                    "state": "unknown",
                    "reason": "Helper work budget exhausted",
                    "valid_for_ms": 500,
                }
                continue
            try:
                results[path] = self.lookup_view(path, view) if self.activity else self.lookup(path)
            except PATH_ERRORS as error:
                # A locked or busy SQLite file, a vanished disk or a changed
                # profile affects this answer only; the next lease retries.
                category = error_category(error)
                failures[category] = failures.get(category, 0) + 1
                results[path] = unavailable()
        exhausted = sum(result["reason"] == "Helper work budget exhausted" for result in results.values())
        if exhausted:
            record("helper", "budget_exhausted", count=exhausted)
        for category, count in failures.items():
            record("helper", "lookup_failed", error=category, count=count)
        if enabled("debug"):
            record(
                "helper",
                "request_completed",
                level="debug",
                count=len(paths),
                elapsed_ms=max(0, (time.monotonic() - (deadline - 0.15)) * 1000),
            )
        return {"version": VERSION, "results": results}


class StatusService(BaseStatusService):
    """Status for the artificial fixture account only (demo and tests)."""

    def __init__(self, settings, store):
        self.settings, self.store = settings, store
        self.config = store.config
        if (
            self.config.expected_drive_id != "fixture-test-drive"
            or self.config.allowed_root_id != "fixture-root"
        ):
            raise ConfigurationError("M2 helper accepts only the artificial fixture account/root")
        self.engine = StatusEngine(self.config, store, read_path=self.prove_fixture_view)

    def prove_fixture_view(self, remote, copy):
        # This is an artificial materialized view, not proof of an rclone mount.
        with SecureRoot(self.settings["visible_root"], self.config.volume_id) as root:
            root.verify(remote.relative_path, copy.size, copy.sha256)
            root.revalidate(self.config.volume_id)
        return True


def serve(settings, store, *, stop=None, tick=None, ready=None, service=None):
    """Multiplex up to 16 bounded peers; evaluate status on the SQLite owner thread.

    Call on the thread owning StateStore. tick/stop/ready are local test hooks,
    never remotely callable operations. No cloud transport is imported here.
    """
    path = Path(settings["socket"])
    directory = private_directory(path.parent, create=True)
    lock = listener = None
    bound = None
    try:
        lock = os.open(
            "helper.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=directory
        )
        info = os.fstat(lock)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
            raise ConfigurationError("Invalid helper lock")
        os.fchmod(lock, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ConfigurationError("A helper already owns this runtime directory") from None
        try:
            old = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            old = None
        if old:
            if not stat.S_ISSOCK(old.st_mode) or old.st_uid != os.getuid():
                raise ConfigurationError("Refusing to replace an unrelated runtime file")
            with socket.socket(socket.AF_UNIX) as probe:
                try:
                    probe.connect(str(path))
                except ConnectionRefusedError:
                    os.unlink(path.name, dir_fd=directory)
                else:
                    raise ConfigurationError("Socket already has a live listener")
        service = service or StatusService(settings, store)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(f"/proc/self/fd/{directory}/{path.name}")
        os.chmod(path.name, 0o600, dir_fd=directory, follow_symlinks=False)
        bound = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        listener.listen(16)
        listener.setblocking(False)
        record("helper", "helper_started", mode=settings["mode"])
        with selectors.DefaultSelector() as selector:
            selector.register(listener, selectors.EVENT_READ)
            clients: dict[socket.socket, tuple[float, bytearray, bytes | None]] = {}

            def discard(connection):
                selector.unregister(connection)
                clients.pop(connection, None)
                connection.close()

            if ready:
                ready()
            try:
                while not (stop and stop()):
                    if tick:
                        tick()
                    for key, _ in selector.select(0.05):
                        connection = key.fileobj
                        assert isinstance(connection, socket.socket)
                        if connection is listener:
                            try:
                                connection, _ = listener.accept()
                            except BlockingIOError:
                                continue
                            try:
                                if len(clients) >= 16 or not same_user(connection):
                                    connection.close()
                                    continue
                                connection.setblocking(False)
                                clients[connection] = (time.monotonic() + TIMEOUT, bytearray(), None)
                                selector.register(connection, selectors.EVENT_READ)
                            except (OSError, CloudMarksError) as error:
                                connection.close()
                                record("helper", "request_rejected", error=error_category(error))
                            continue
                        deadline, incoming, outgoing = clients[connection]
                        try:
                            if time.monotonic() >= deadline:
                                raise TimeoutError("Protocol deadline exceeded")
                            if outgoing is not None:
                                sent = connection.send(outgoing)
                                if sent == 0:
                                    raise ConnectionError("Peer closed response stream")
                                outgoing = outgoing[sent:]
                                if not outgoing:
                                    discard(connection)
                                else:
                                    clients[connection] = (deadline, incoming, outgoing)
                                continue
                            chunk = connection.recv(min(4096, MAX_FRAME + 1 - len(incoming)))
                            if not chunk:
                                raise ValueError("Incomplete protocol frame")
                            incoming.extend(chunk)
                            if len(incoming) > MAX_FRAME:
                                raise ValueError("Protocol frame too large")
                            if b"\n" not in incoming:
                                continue
                            if not incoming.endswith(b"\n") or incoming.count(b"\n") != 1:
                                raise ValueError("Expected one bounded frame")
                            # Only framing is multiplexed. The owning thread
                            # still evaluates every status and SQLite read.
                            # UTF-8 avoids expanding a valid near-limit Unicode
                            # request path into an oversized response key.
                            outgoing = encode(service.handle(json.loads(incoming)), ensure_ascii=False)
                            clients[connection] = (deadline, incoming, outgoing)
                            selector.modify(connection, selectors.EVENT_WRITE)
                        except BlockingIOError:
                            continue
                        except (
                            OSError,
                            ValueError,
                            TypeError,
                            KeyError,
                            sqlite3.Error,
                            CloudMarksError,
                        ) as error:
                            record("helper", "request_rejected", error=error_category(error))
                            discard(connection)
                    for connection, (deadline, _, _) in list(clients.items()):
                        if time.monotonic() >= deadline:
                            discard(connection)
            finally:
                for connection in list(clients):
                    discard(connection)

    finally:
        if listener:
            listener.close()
        if bound:
            try:
                current = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
                if (current.st_dev, current.st_ino) == (bound.st_dev, bound.st_ino):
                    os.unlink(path.name, dir_fd=directory)
            except FileNotFoundError:
                pass
        if lock is not None:
            os.close(lock)
        os.close(directory)
        record("helper", "helper_stopped")


def run(settings):
    if settings["mode"] == "onedrive":
        from .offline_config import load_offline
        from .state import ReadStore

        config, offline = load_offline(settings["core_config"])
        with ReadStore(config) as store:
            activity = None
            folders = None
            try:
                if settings["version"] in (3, 4):
                    from .activity import Activity

                    activity = Activity(config, offline, settings).start()
                if activity:
                    from .folders import FolderObserver

                    folders = FolderObserver(config, offline, activity).start()
                service = OfflineStatusService(settings, store, offline, activity=activity, folders=folders)
                try:
                    serve(settings, store, service=service)
                finally:
                    service.close()
            finally:
                if folders:
                    folders.close()
                if activity:
                    activity.close()
        return
    config = load_config(settings["core_config"])
    with StateStore(config) as store:
        serve(settings, store)


class OfflineStatusService(BaseStatusService):
    """Read-only account status. This class never constructs transport or reads credentials;
    the optional activity/folder workers passed in do (see the module docstring)."""

    supports_large_files = True

    def __init__(self, settings, store, offline, *, activity=None, folders=None):
        from .verification import status_engine

        self.activity = activity
        self.folders = folders
        if activity:
            from .activity import ObservedStore

            store = ObservedStore(store, activity)
        self.settings, self.store, self.config = settings, store, store.config
        if settings["visible_root"] != offline["visible_root"]:
            raise ConfigurationError("Desktop and offline view roots disagree")
        self.engine = status_engine(
            store.config,
            store,
            offline,
            background=True,
            report=lambda category: record("helper", "lookup_failed", error=category),
        )

    def close(self):
        memo = getattr(self.engine, "memo", None)
        if memo is not None and hasattr(memo, "close"):
            memo.close()

    def lookup_view(self, path, view):
        assert self.activity is not None
        relative_parts(path)
        remote, result = self.activity.evidence(path)
        if remote is None:
            return result
        if remote.is_directory:
            return (
                self.folders.request(path, view)
                if self.folders
                else self.activity.result("unknown", "Folder verification is unavailable")
            )
        if view == "upload":
            return result
        row = self.store.file_at(path)
        # A remote-only observation must never hide an older retained file at
        # this path, including replacement identities and renamed publications.
        entry = self.store.local_entry_at(path)
        if entry and (entry["item_id"] != remote.identity.item_id or not row):
            return self.activity.result(
                "unknown", "Retained path differs from OneDrive; preserve the local copy"
            )
        if not row or row["item_id"] != remote.identity.item_id:
            return result
        checked = self.lookup(path)
        checked["valid_for_ms"] = min(checked["valid_for_ms"], result["valid_for_ms"])
        return checked

    def lookup(self, path):
        result = super().lookup(path)
        if result["state"] in ("kept_offline", "saved_locally"):
            # The status lease must not outlive explicit large-file proof.
            row = self.store.file_at(path)
            if row and row["size"] > MAX_VERIFY_BYTES:
                from .verification import PROOF_SECONDS

                proof = self.store.verification_payload(
                    FileIdentity(self.config.expected_drive_id, row["item_id"])
                )
                remaining = (
                    0 if proof is None else PROOF_SECONDS - (time.time() - json.loads(proof)["verified_at"])
                )
                if remaining <= 0:
                    result.update(state="unknown", reason="Verification evidence expired; run offline verify")
                else:
                    result["valid_for_ms"] = max(1, min(result["valid_for_ms"], int(remaining * 1000)))
        if result["state"] == "transferring":
            # An interrupted writer cannot remain active merely because the
            # display helper deliberately never writes recovery state.
            fd = os.open(str(self.config.state_path) + ".lock", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
                    raise ConfigurationError("State writer lock is unavailable or unsafe")
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    pass
                else:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                    result.update(
                        state="error", reason="Transfer writer stopped; run offline recover before retry"
                    )
            finally:
                os.close(fd)
        return result
