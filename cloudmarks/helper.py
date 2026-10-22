"""Single-owner local status service. IPC exposes no transfer or state writes."""

import fcntl
import json
import os
import socket
import stat
import time
from pathlib import Path

from .config import load_config
from .engine import StatusEngine
from .ipc import LEASE_MS, MAX_BATCH, VERSION, encode, private_directory, receive, same_user
from .model import CloudMarksError, ConfigurationError, FileIdentity
from .paths import SecureRoot, relative_parts
from .state import StateStore
from .telemetry import enabled, error_category, record

MAX_VERIFY_BYTES = 8 * 1024 * 1024


class StatusService:
    supports_large_files = False

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

    def lookup(self, path):
        relative_parts(path)
        row = self.store.connection.execute(
            "SELECT item_id FROM files WHERE drive_id=? AND path_key=?",
            (self.config.expected_drive_id, self.config.path_key(path)),
        ).fetchone()
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
        result.update(
            state=status.state.value,
            reason=status.reason,
            valid_for_ms=LEASE_MS
            if status.state.value == "unknown"
            else max(1, min(LEASE_MS, int(remaining * 1000))),
        )
        return result

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
        if view not in ("offline", "upload") or (view == "upload" and not getattr(self, "activity", None)):
            raise ValueError("Unsupported status view")
        paths = value["paths"]
        for path in paths:
            relative_parts(path)
        if len(set(paths)) != len(paths):
            raise ValueError("Duplicate request paths")
        deadline = time.monotonic() + 0.15
        results = {}
        for path in paths:
            if time.monotonic() >= deadline:
                results[path] = {
                    "state": "unknown",
                    "reason": "Helper work budget exhausted",
                    "valid_for_ms": 500,
                }
            else:
                results[path] = (
                    self.lookup_view(path, view) if getattr(self, "activity", None) else self.lookup(path)
                )
        exhausted = sum(result["reason"] == "Helper work budget exhausted" for result in results.values())
        if exhausted:
            record("helper", "budget_exhausted", count=exhausted)
        if enabled("debug"):
            record(
                "helper",
                "request_completed",
                level="debug",
                count=len(paths),
                elapsed_ms=max(0, (time.monotonic() - (deadline - 0.15)) * 1000),
            )
        return {"version": VERSION, "results": results}


def serve(settings, store, *, stop=None, tick=None, ready=None, service=None):
    """One bounded request at a time; a stuck disk can stall only this process.

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
        listener.listen(8)
        listener.settimeout(0.1)
        record("helper", "helper_started", mode=settings["mode"])
        if ready:
            ready()
        while not (stop and stop()):
            if tick:
                tick()
            try:
                connection, _ = listener.accept()
            except TimeoutError:
                continue
            with connection:
                try:
                    if not same_user(connection):
                        continue
                    response = service.handle(receive(connection))
                    connection.sendall(encode(response))
                except (OSError, ValueError, TypeError, KeyError, CloudMarksError) as error:
                    # Close malformed requests without paths or raw private error text.
                    record("helper", "request_rejected", error=error_category(error))
                    continue
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
            try:
                if settings["version"] == 3:
                    from .activity import Activity

                    activity = Activity(config, offline, settings).start()
                service = OfflineStatusService(settings, store, offline, activity=activity)
                serve(settings, store, service=service)
            finally:
                if activity:
                    activity.close()
        return
    config = load_config(settings["core_config"])
    with StateStore(config) as store:
        serve(settings, store)


class OfflineStatusService(StatusService):
    """Read-only account status: no transport construction or credential reads."""

    supports_large_files = True

    def __init__(self, settings, store, offline, *, activity=None):
        from .verification import status_engine

        self.activity = activity
        if activity:
            from .activity import ObservedStore

            store = ObservedStore(store, activity)
        self.settings, self.store, self.config = settings, store, store.config
        if settings["visible_root"] != offline["visible_root"]:
            raise ConfigurationError("Desktop and offline view roots disagree")
        self.engine = status_engine(store.config, store, offline)

    def lookup_view(self, path, view):
        relative_parts(path)
        remote, result = self.activity.evidence(path)
        if remote is None:
            return result
        if remote.is_directory:
            return self.activity.result("unknown", "Folder coverage requires an explicit offline manifest")
        if view == "upload":
            return result
        row = self.store.connection.execute(
            "SELECT item_id FROM files WHERE drive_id=? AND path_key=?",
            (self.config.expected_drive_id, self.config.path_key(path)),
        ).fetchone()
        # A remote-only observation must never hide an older retained file at
        # this path, including replacement identities and renamed publications.
        from .activity import LIMIT

        entries = self.store.connection.execute(
            "SELECT item_id,relative_path FROM local_entries LIMIT ?", (LIMIT + 1,)
        ).fetchall()
        if len(entries) > LIMIT:
            return self.activity.result("unknown", "Retained catalog exceeds the automatic observation limit")
        entry = next(
            (e for e in entries if self.config.path_key(e["relative_path"]) == self.config.path_key(path)),
            None,
        )
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
            row = self.store.connection.execute(
                "SELECT item_id,size FROM files WHERE drive_id=? AND path_key=?",
                (self.config.expected_drive_id, self.config.path_key(path)),
            ).fetchone()
            if row and row["size"] > MAX_VERIFY_BYTES:
                from .verification import PROOF_SECONDS

                proof = self.store.connection.execute(
                    "SELECT payload FROM verifications WHERE drive_id=? AND item_id=?",
                    (self.config.expected_drive_id, row["item_id"]),
                ).fetchone()
                remaining = (
                    0
                    if proof is None
                    else PROOF_SECONDS - (time.time() - json.loads(proof[0])["verified_at"])
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
