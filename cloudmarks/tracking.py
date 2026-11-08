"""Explicit read-only change checks, durable jobs and bounded folder intent.

No scheduler, cloud writes, automatic merge, conflict resolution or GC. A
blocked job retains its original publication and an independently verified
remote candidate. All byte-changing operations retain M3's unmounted gate.
"""

import json
import os
import sqlite3
import time
from dataclasses import asdict, replace

from .limits import TREE_ENTRIES
from .model import Cancelled, FileIdentity, IntegrityError, LocalCopy, RemoteMissing, ScopeError, StorageError
from .paths import SecureRoot, relative_parts
from .telemetry import error_category, record

RESUME_REASONS = {
    "storage": "Check account, disk and connection; resume explicitly",
    "socket": "Check account, disk and connection; resume explicitly",
    "timeout": "Connection or disk timed out; resume explicitly",
    "database": "State database was busy; resume explicitly",
    "integrity": "Downloaded or local bytes failed verification; previous data kept; resume explicitly",
    "scope": "Path, account or selection changed; review it, then resume explicitly",
    "configuration": "Check the offline profile and account; resume explicitly",
    "permissions": "Disk or state permissions changed; fix them, then resume explicitly",
    "unexpected": "Unexpected failure; see cloudmarks logs, then resume explicitly",
}


def manifest(observations):
    return sorted(
        [
            {
                "item_id": value.remote.identity.item_id,
                "path": value.remote.relative_path,
                "revision": value.remote.revision,
                "size": value.remote.size,
                "directory": value.remote.is_directory,
                "kind": value.kind,
                "digest": value.digest.hex(),
            }
            for value in observations
        ],
        key=lambda value: value["path"],
    )


class ChangeTracker:
    def __init__(self, controller):
        self.control = controller
        self.config, self.store, self.backend = controller.config, controller.store, controller.backend

    def identity_at(self, path):
        try:
            return self.control.identity_at(path)
        except ScopeError:
            relative_parts(path)
            identity = self.store.retained_identity_at(path)
            if identity is None:
                raise
            return identity

    def issue(self, remote, reason, candidate=None):
        old = self.store.issue(remote.identity)
        if (
            candidate is None
            and old
            and (old["revision"], old["relative_path"]) == (remote.revision, remote.relative_path)
        ):
            candidate = json.loads(old["candidate"]) if old["candidate"] else None
        self.store.record_issue(remote, reason, candidate)
        self.store.fail_transfer(remote.identity, reason)

    def observe(self, identity):
        previous = self.store.remote(identity)
        if previous is None:
            raise ScopeError("Stable identity has no prior observation")
        try:
            observation = self.backend.describe_identity(identity)
        except RemoteMissing:
            # A 404 can mean deletion or loss of access. Never assert that it
            # proves a deletion, and never evict or reuse the old pathname.
            remote = replace(previous, deleted=True, observed_at=time.time())
            self.store.observe(remote)
            self.issue(
                remote, "Remote item unavailable (deleted or inaccessible); retain snapshot and review"
            )
            return None
        remote = observation.remote
        self.config.validate_remote(remote)
        if remote.identity != identity:
            raise ScopeError("Refresh returned another stable identity")
        try:
            self.store.observe(remote)
        except sqlite3.IntegrityError:
            self.issue(
                previous, "Remote path collides with another tracked item; retain all snapshots and review"
            )
            raise ScopeError("Remote path collision requires review") from None
        return observation

    def assess(self, observation):
        remote = observation.remote
        row = self.store.row(remote.identity)
        entry = self.control.entry(remote.identity)
        reason = None
        if entry:
            try:
                with SecureRoot(self.config.storage_root, self.config.volume_id) as root:
                    self.control.verify_entry(root, entry)
                    root.revalidate(self.config.volume_id)
            except (OSError, IntegrityError, ScopeError):
                self.store.set_dirty(remote.identity, True)
                reason = "Local publication changed or is missing; retain it and review both versions"
            if entry["path"] != remote.relative_path:
                reason = "Remote rename detected; retained path is unchanged; review both paths"
        if row["dirty"]:
            reason = "Modified local data requires review; no automatic replacement"
        if self.store.issue(remote.identity) and reason is None:
            reason = "Earlier change remains unresolved; review retained versions"
        if reason:
            self.issue(remote, reason)
        return reason

    def enqueue(self, remote, *, direct=True):
        if remote.is_directory or remote.deleted:
            raise ScopeError("Only observed live files can be queued")
        if direct:
            self.store.set_pin(remote.identity, True)
        self.store.enqueue_job(remote)

    def queue(self, path):
        self.control.recover()
        try:
            identity = self.identity_at(path)
        except ScopeError:
            observation = self.backend.describe(path)
            self.store.observe(observation.remote)
            identity = observation.remote.identity
        self.enqueue(self.store.remote(identity))

    def refresh(self, path):
        self.control.recover()
        identity = self.identity_at(path)
        observation = self.observe(identity)
        if observation is None:
            return
        remote = observation.remote
        if remote.is_directory:
            raise ScopeError("Refresh a recursive folder with queue-tree to replace its complete manifest")
        self.assess(observation)
        if self.store.row(identity)["pinned"]:
            self.enqueue(remote, direct=False)

    def candidate(self, observation):
        remote = observation.remote
        issue = self.store.issue(remote.identity)
        if (
            issue
            and issue["candidate"]
            and (issue["revision"], issue["relative_path"]) == (remote.revision, remote.relative_path)
        ):
            copy = json.loads(issue["candidate"])
            with SecureRoot(self.config.storage_root, self.config.volume_id) as root:
                root.verify(copy["object_path"], copy["size"], copy["sha256"])
                expected = observation.hasher()
                with root.open_regular(copy["object_path"]) as fd:
                    while block := os.read(fd, 256 * 1024):
                        self.control.check_cancelled()
                        expected.update(block)
                root.revalidate(self.config.volume_id)
            if copy["size"] == remote.size and expected.digest() == observation.digest:
                return copy
        self.control.guard()
        with SecureRoot(self.config.storage_root, self.config.volume_id) as root:
            reserve = 2 * max(4096, ((remote.size + 4095) // 4096) * 4096) + 65536
            try:
                self.control.reserve_budget(root, reserve)
            except StorageError:
                raise StorageError(
                    "Conflict candidate exceeds the storage budget; original data retained"
                ) from None
            self.control.space(root, reserve)
            path, sha = self.control.download(root, observation)
            final = self.backend.describe_identity(remote.identity)
            if not observation.same_version(final):
                raise IntegrityError("Remote conflict candidate changed; preserve partial bytes and retry")
            self.control.check_cancelled()
            self.control.guard()
            root.revalidate(self.config.volume_id)
            path = root.publish(path)
            with root.open_regular(path) as fd:
                os.fchmod(fd, 0o444)
                os.fsync(fd)
            root.verify(path, remote.size, sha)
            root.revalidate(self.config.volume_id)
            copy = asdict(LocalCopy(remote.identity, remote.revision, path, remote.size, sha))
            self.store.record_snapshot(path, remote, sha)
            return copy

    def current(self, observation):
        """Reuse a complete publication only after fresh server checksum proof."""
        remote = observation.remote
        copy = self.store.copy(remote.identity)
        if copy is None or (copy.revision, copy.size) != (remote.revision, remote.size):
            return False
        expected = observation.hasher()
        with SecureRoot(self.config.storage_root, self.config.volume_id) as root:
            self.control.verify_entry(root, self.control.entry(remote.identity))
            with root.open_regular(copy.object_path) as fd:
                while block := os.read(fd, 256 * 1024):
                    self.control.check_cancelled()
                    expected.update(block)
            root.revalidate(self.config.volume_id)
        if expected.digest() != observation.digest:
            return False
        self.store.observe(replace(remote, sha256=copy.sha256))
        self.store.mark_idle(remote.identity)
        return True

    def resume(self, *, limit=TREE_ENTRIES):
        if type(limit) is not int or not 1 <= limit <= TREE_ENTRIES:
            raise ScopeError(f"Worker limit must be between 1 and {TREE_ENTRIES}")
        self.control.recover()
        jobs = self.store.resumable_jobs(limit)
        for job in jobs:
            self.control.check_cancelled()
            identity = FileIdentity(job["drive_id"], job["item_id"])
            self.config.validate_identity(identity)
            if not self.store.row(identity)["pinned"]:
                self.store.set_job(identity, "cancelled")
                continue
            self.store.set_job(identity, "running")
            try:
                observation = self.observe(identity)
                reason = (
                    self.assess(observation) if observation else "Remote item unavailable; review snapshot"
                )
                if observation and observation.remote.relative_path != job["requested_path"]:
                    reason = "Queued path was renamed; preserve snapshots and review both paths"
                    self.issue(observation.remote, reason)
                if reason:
                    if observation and not observation.remote.is_directory:
                        copy = self.candidate(observation)
                        self.issue(observation.remote, reason, copy)
                    self.store.set_job(identity, "blocked", reason)
                    continue
                if observation.remote.is_directory:
                    raise ScopeError("Queued file became a folder; review its intent")
                if not self.current(observation):
                    self.control.keep(observation.remote.relative_path, pin=False)
                self.store.set_job(identity, "done")
            except Cancelled:
                self.store.set_job(identity, "pending", "Cancelled; resume explicitly")
                raise
            except Exception as error:
                category = error_category(error)
                self.control.progress(phase="retryable_error", error_type=type(error).__name__)
                record("cli", "queue_failed", error=category)
                # Failures stay retryable; persisted text is a fixed message per
                # cause, never private adapter diagnostics.
                self.store.set_job(
                    identity, "error", RESUME_REASONS.get(category, RESUME_REASONS["unexpected"])
                )
        return self.jobs()

    def jobs(self):
        return self.store.report("jobs")

    def snapshots(self):
        return self.store.report("snapshots")

    def changes(self):
        return self.store.report("changes")

    def conflicts(self):
        return self.store.report("conflicts")

    def queue_tree(self, path, *, limit=TREE_ENTRIES):
        self.control.recover()
        first = self.backend.tree(path, limit=limit, cancelled=self.control.cancelled)
        second = self.backend.tree(path, limit=limit, cancelled=self.control.cancelled)
        description = manifest(first)
        if not first or len(first) > limit or description != manifest(second):
            raise IntegrityError("Folder changed during enumeration; previous intent retained")
        roots = [
            value for value in second if value.remote.relative_path == path and value.remote.is_directory
        ]
        if len(roots) != 1:
            raise ScopeError("Manifest has no unique requested folder")
        if len({v.remote.identity for v in second}) != len(second) or len(
            {self.config.path_key(v.remote.relative_path) for v in second}
        ) != len(second):
            raise ScopeError("Manifest contains duplicate identities or paths")
        for value in second:
            remote = value.remote
            self.config.validate_remote(remote)
            if remote.relative_path != path and not remote.relative_path.startswith(path + "/"):
                raise ScopeError("Manifest escaped the requested folder")
            if (
                self.control.entry(remote.identity)
                and self.control.entry(remote.identity)["path"] != remote.relative_path
            ):
                raise ScopeError("Resolve retained renames before replacing folder intent")
        # All-or-nothing metadata, membership and queue intent; no network or
        # filesystem publication happens in this transaction.
        self.control.check_cancelled()
        self.store.replace_folder_intent(roots[0].remote.identity, description, [v.remote for v in second])
        return second

    def unpin_tree(self, path):
        self.control.recover()
        self.store.release_folder_intent(self.identity_at(path))

    def check_online(self):
        """Do not let a stale local-first path shadow changed cloud content."""
        found = False
        for identity in self.store.retained_identities():
            found = True
            observation = self.observe(identity)
            if not observation or self.assess(observation):
                raise ScopeError("Changed retained snapshot requires offline conflicts review")
            copy, remote = self.store.copy(identity), observation.remote
            if (copy.revision, copy.size) != (remote.revision, remote.size):
                if self.store.row(identity)["pinned"]:
                    self.enqueue(remote, direct=False)
                raise ScopeError("Remote revision changed; run offline resume before online mounting")
            check = observation.hasher()
            with SecureRoot(self.config.storage_root, self.config.volume_id) as root:
                with root.open_regular(copy.object_path) as fd:
                    while block := os.read(fd, 256 * 1024):
                        check.update(block)
                root.revalidate(self.config.volume_id)
            if check.digest() != observation.digest:
                self.issue(remote, "Remote checksum differs; retain snapshot and review")
                raise IntegrityError("Remote checksum mismatch prevents online mounting")
            self.backend.verify_transport_item(observation)
            self.store.observe(replace(remote, sha256=copy.sha256))
        if not found:
            self.backend.verify_transport_root()
