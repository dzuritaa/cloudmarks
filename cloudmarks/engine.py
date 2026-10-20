"""Evidence-based status calculation. Invoke in a helper, never a UI callback."""

import math
import time

from .model import FileStatus, IntegrityError, State
from .paths import SecureRoot


class StatusEngine:
    def __init__(self, config, store, *, read_path=None, clock=time.time):
        self.config = config
        self.store = store
        self.read_path = read_path or (lambda remote, copy: False)
        self.clock = clock

    def status(self, identity):
        try:
            if self.config != self.store.config:
                return FileStatus(State.UNKNOWN, "Helper configuration disagrees with its state binding")
            self.config.validate_identity(identity)
            remote = self.store.remote(identity)
            if remote is None:
                return FileStatus(State.UNKNOWN, "No verified remote observation")
            age = self.clock() - remote.observed_at
            if not math.isfinite(age) or age < 0 or age > self.config.max_age_seconds:
                return FileStatus(State.UNKNOWN, "Remote observation needs checking")
            if self.store.connection.execute("PRAGMA user_version").fetchone()[0] >= 3:
                issue = self.store.connection.execute(
                    "SELECT reason FROM issues WHERE drive_id=? AND item_id=?", self.store.key(identity)
                ).fetchone()
                if issue:
                    return FileStatus(State.ERROR, issue[0])
            if remote.deleted:
                return FileStatus(State.UNKNOWN, "Remote deletion observed; preserve any local snapshot")
            if remote.is_directory:
                return self.folder_status(remote)
            with SecureRoot(self.config.storage_root, self.config.volume_id) as root:
                row = self.store.row(identity)
                if row["dirty"]:
                    return FileStatus(State.ERROR, "Unsent or modified local data needs attention")
                if row["operation"] == "error":
                    return FileStatus(State.ERROR, row["error"] or "Operation failed")
                if row["operation"] == "active":
                    return FileStatus(State.TRANSFERRING, "A tracked download is active")
                copy = self.store.copy(identity)
                if copy is None:
                    return FileStatus(State.CLOUD_ONLY, "Known remote file; no verified local copy")
                if (
                    copy.revision != remote.revision
                    or copy.size != remote.size
                    or (remote.sha256 is not None and copy.sha256 != remote.sha256)
                ):
                    return FileStatus(State.UNKNOWN, "Local snapshot is not the observed remote version")
                verified_fingerprint = root.verify(copy.object_path, copy.size, copy.sha256)
                if not self.read_path(remote, copy):
                    return FileStatus(State.UNKNOWN, "Supported offline open path is not verified")
                root.revalidate(self.config.volume_id)
                if root.current_fingerprint(copy.object_path) != verified_fingerprint:
                    raise IntegrityError("Local content changed after verification")
                latest = self.store.remote(identity)
                current = self.store.row(identity)
                if (
                    latest != remote
                    or self.store.copy(identity) != copy
                    or current["dirty"]
                    or current["operation"] != "idle"
                ):
                    return FileStatus(State.UNKNOWN, "State changed while availability was being checked")
                age = self.clock() - remote.observed_at
                if not math.isfinite(age) or not 0 <= age <= self.config.max_age_seconds:
                    return FileStatus(State.UNKNOWN, "Remote evidence expired during verification")
                return FileStatus(
                    State.KEPT_OFFLINE if current["pinned"] else State.SAVED_LOCALLY,
                    "Complete local bytes, revision and offline read path verified",
                )
        except IntegrityError:
            return FileStatus(State.ERROR, "Local content changed or failed verification; preserve it")
        except Exception:
            return FileStatus(State.UNKNOWN, "Account, storage, path or state evidence is unavailable")

    def folder_status(self, remote):
        import json

        if self.store.connection.execute("PRAGMA user_version").fetchone()[0] < 3:
            return FileStatus(State.UNKNOWN, "Recursive folder coverage is not verified")
        row = self.store.connection.execute(
            "SELECT * FROM folder_intents WHERE drive_id=? AND item_id=?", self.store.key(remote.identity)
        ).fetchone()
        if row is None or not 0 <= self.clock() - row["scanned_at"] <= self.config.max_age_seconds:
            return FileStatus(State.UNKNOWN, "Folder manifest is missing or expired; run queue-tree")
        members = json.loads(row["manifest"])
        files = [value for value in members if not value["directory"]]
        # A fixed helper budget: no directory walking, cloud requests or
        # unbounded child hashing in the status-only IPC process.
        if (
            not files
            or len(members) > 128
            or len(files) > 64
            or sum(v["size"] for v in files) > 8 * 1024 * 1024
        ):
            return FileStatus(State.UNKNOWN, "Folder is empty or exceeds the aggregate verification budget")
        from .model import FileIdentity

        states = []
        for value in members:
            identity = FileIdentity(self.config.expected_drive_id, value["item_id"])
            current = self.store.remote(identity)
            if (
                current is None
                or current.deleted
                or (current.relative_path, current.revision, current.size, current.is_directory)
                != (value["path"], value["revision"], value["size"], value["directory"])
            ):
                return FileStatus(
                    State.UNKNOWN, "Folder manifest no longer matches tracked members; run queue-tree"
                )
            if not current.is_directory:
                states.append(self.status(identity).state)
        latest = self.store.connection.execute(
            "SELECT manifest,scanned_at,pinned FROM folder_intents WHERE drive_id=? AND item_id=?",
            self.store.key(remote.identity),
        ).fetchone()
        if latest is None or tuple(latest) != tuple(row[key] for key in ("manifest", "scanned_at", "pinned")):
            return FileStatus(State.UNKNOWN, "Folder intent changed while checking")
        if not 0 <= self.clock() - row["scanned_at"] <= self.config.max_age_seconds:
            return FileStatus(State.UNKNOWN, "Folder manifest expired while checking")
        if State.ERROR in states:
            return FileStatus(
                State.ERROR, "A recorded folder member needs review; use offline jobs/conflicts"
            )
        if State.TRANSFERRING in states:
            return FileStatus(State.TRANSFERRING, "A recorded folder member is downloading")
        verified = sum(state in (State.KEPT_OFFLINE, State.SAVED_LOCALLY) for state in states)
        if verified != len(files):
            return FileStatus(
                State.UNKNOWN, f"Verified {verified}/{len(files)} recorded files; resume or refresh folder"
            )
        pinned = bool(self.store.row(remote.identity)["pinned"])
        return FileStatus(
            State.KEPT_OFFLINE if pinned else State.SAVED_LOCALLY,
            f"All {verified} files in the bounded manifest have verified read paths",
        )
