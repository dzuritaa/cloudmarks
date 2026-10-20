"""Explicit, local-only conflict retirement with durable receipts and export.

No merge, upload, cloud deletion or garbage collection. Filesystem intent is
rolled back after interruption; a committed receipt owns every retained version.
"""

import hashlib
import json
import os
import time
import uuid
from dataclasses import asdict
from pathlib import Path

from .model import IntegrityError, ScopeError, StorageError
from .offline import exists, rename
from .onedrive import Cancelled
from .paths import SecureRoot, fingerprint, open_absolute_directory
from .tracking import ChangeTracker


def measure(root, path, cancelled=lambda: False, *, progress=lambda **values: None):
    """Measure actual bytes, including damaged/modified versions, without trusting the index."""
    if cancelled():
        raise Cancelled("Local inspection cancelled; all versions retained")
    if not exists(root, path):
        return {"path": path, "missing": True}
    with root.open_regular(path) as fd:
        before = fingerprint(os.fstat(fd))
        digest, size = hashlib.sha256(), 0
        while block := os.read(fd, 1024 * 1024):
            if cancelled():
                raise Cancelled("Local inspection cancelled; all versions retained")
            digest.update(block)
            size += len(block)
            progress(completed=size, total=before[2])
        if before != fingerprint(os.fstat(fd)) or before != root.current_fingerprint(path):
            raise IntegrityError("Content changed during archive inspection; preserve it")
    return {"path": path, "size": size, "sha256": digest.hexdigest(), "fingerprint": list(before)}


def unchanged(root, source):
    if source.get("missing"):
        if exists(root, source["path"]):
            raise IntegrityError("An absent version appeared during inspection; preserve it")
    elif list(root.current_fingerprint(source["path"])) != source["fingerprint"]:
        raise IntegrityError("A retained version changed during inspection; preserve it")


def rollback_archive(control, root, identity, payload):
    if control.entry(identity) != payload["old"]:
        raise IntegrityError("Archive recovery disagrees with committed publication")
    source, target = payload["source"], payload["target"]
    if source["path"] != "local/" + payload["old"]["path"] or not target.startswith("recovery/"):
        raise ScopeError("Invalid archive recovery location")
    if exists(root, target):
        if source.get("missing"):
            raise IntegrityError("Unexpected archive appeared; preserve it")
        root.verify(target, source["size"], source["sha256"])
        rename(root, target, source["path"])
    elif source.get("missing"):
        unchanged(root, source)
    else:
        # A prior rollback may have restored the name before it died. Rename
        # changes ctime, so idempotent recovery proves bytes rather than old stat.
        root.verify(source["path"], source["size"], source["sha256"])


class ConflictRecovery:
    def __init__(self, controller):
        self.control = controller
        self.config, self.store = controller.config, controller.store

    def archives(self):
        return [
            json.loads(row[0])
            for row in self.store.connection.execute("SELECT payload FROM archives ORDER BY archive_id")
        ]

    def resolve(self, path, *, retry_remote=False):
        control = self.control
        control.recover()
        identity = ChangeTracker(control).identity_at(path)
        key = self.store.key(identity)
        issue = self.store.connection.execute(
            "SELECT * FROM issues WHERE drive_id=? AND item_id=?", key
        ).fetchone()
        if issue is None:
            raise ScopeError("No recorded conflict at this path; inspect offline conflicts first")
        remote, old = self.store.remote(identity), control.entry(identity)
        if remote.is_directory:
            raise ScopeError("Resolve retained files individually")
        observation = None
        if retry_remote:
            observation = control.backend.describe_identity(identity)
            self.config.validate_remote(observation.remote)
            if (
                observation.remote.identity != identity
                or observation.remote.deleted
                or observation.remote.is_directory
            ):
                raise ScopeError("Retry requires the same live, scoped file identity")
        archive_id = uuid.uuid4().hex
        target = "recovery/" + archive_id + ".local"
        with SecureRoot(self.config.storage_root, self.config.volume_id) as root:
            sources = {}
            if old:
                sources["local"] = measure(root, "local/" + old["path"], control.cancelled)
                sources["original"] = measure(root, old["copy"]["object_path"], control.cancelled)
            candidate = json.loads(issue["candidate"]) if issue["candidate"] else None
            if candidate:
                sources["remote_candidate"] = measure(root, candidate["object_path"], control.cancelled)
            receipt = {
                "version": 1,
                "archive_id": archive_id,
                "created_at": time.time(),
                "remote": asdict(remote),
                "entry": old,
                "issue": dict(issue),
                "sources": sources,
                "pin_sources": [
                    r[0]
                    for r in self.store.connection.execute(
                        "SELECT owner FROM pin_sources WHERE drive_id=? AND item_id=?", key
                    )
                ],
                "outcome": "retry_queued" if retry_remote else "retired",
            }
            if observation:
                latest = control.backend.describe_identity(identity)
                if not observation.same_version(latest):
                    raise ScopeError("Remote changed during review; inspect again")
                observation = latest
            control.guard()
            root.revalidate(self.config.volume_id)
            for source in sources.values():
                unchanged(root, source)
            if control.cancelled():
                raise Cancelled("Archive cancelled; all versions retained")
            if exists(root, target):
                raise ScopeError("Archive destination already exists; preserve it")
            payload = None
            try:
                if old:
                    payload = {"action": "archive", "old": old, "source": sources["local"], "target": target}
                    control.journal(identity, payload)
                    control.hook("archive-intent")
                    if not sources["local"].get("missing"):
                        unchanged(root, sources["local"])
                        rename(root, sources["local"]["path"], target)
                        verified = root.verify(target, sources["local"]["size"], sources["local"]["sha256"])
                        sources["local"] = dict(sources["local"], path=target, fingerprint=list(verified))
                    control.hook("archive-visible")
                root.revalidate(self.config.volume_id)
                control.guard()
                for source in sources.values():
                    unchanged(root, source)
                with self.store.transaction() as connection:
                    connection.execute("INSERT INTO archives VALUES (?,?)", (archive_id, json.dumps(receipt)))
                    for table in ("local_entries", "copies", "issues", "verifications", "jobs"):
                        connection.execute(f"DELETE FROM {table} WHERE drive_id=? AND item_id=?", key)
                    if observation:
                        self.store.observe(observation.remote, connection=connection)
                        connection.execute(
                            "UPDATE files SET dirty=0,operation='idle',error=NULL WHERE drive_id=? AND item_id=?",
                            key,
                        )
                        # Preserve existing folder/direct intent; explicitly retrying an unpinned file pins it.
                        if not receipt["pin_sources"]:
                            connection.execute("INSERT INTO pin_sources VALUES (?,?,'direct')", key)
                        connection.execute("UPDATE files SET pinned=1 WHERE drive_id=? AND item_id=?", key)
                        connection.execute(
                            "INSERT INTO jobs(drive_id,item_id,requested_path) VALUES (?,?,?)",
                            (*key, observation.remote.relative_path),
                        )
                    else:
                        connection.execute("DELETE FROM pin_sources WHERE drive_id=? AND item_id=?", key)
                        connection.execute("DELETE FROM files WHERE drive_id=? AND item_id=?", key)
                    connection.execute("DELETE FROM publications WHERE drive_id=? AND item_id=?", key)
                control.hook("archive-committed")
            except Exception:
                if payload:
                    control.recover()
                raise
        return receipt

    def export(self, archive_id, destination):
        """Copy an archived bundle to a new private directory; never overwrite or delete sources."""
        self.control.guard()
        if self.control.cancelled():
            raise Cancelled("Export cancelled; all versions retained")
        row = self.store.connection.execute(
            "SELECT payload FROM archives WHERE archive_id=?", (archive_id,)
        ).fetchone()
        if row is None:
            raise ScopeError("Unknown archive ID")
        receipt = json.loads(row[0])
        destination = Path(destination)
        if not destination.is_absolute():
            raise ScopeError("Export requires a new absolute destination directory")
        for forbidden in (
            self.config.storage_root,
            Path(self.control.settings["visible_root"]),
            self.config.state_path.parent,
        ):
            if destination == forbidden or forbidden in destination.parents:
                raise ScopeError("Export must be outside the managed store, view and state directory")
        parent = open_absolute_directory(destination.parent)
        directory = None
        try:
            os.mkdir(destination.name, 0o700, dir_fd=parent)
            directory = os.open(destination.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.fchmod(directory, 0o700)
            os.fsync(parent)
            with SecureRoot(self.config.storage_root, self.config.volume_id) as root:
                for label, source in receipt["sources"].items():
                    if source.get("missing"):
                        continue
                    with root.open_regular(source["path"]) as src:
                        before = fingerprint(os.fstat(src))
                        fd = os.open(
                            label + ".bin",
                            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                            0o600,
                            dir_fd=directory,
                        )
                        digest, count = hashlib.sha256(), 0
                        with os.fdopen(fd, "wb") as output:
                            while block := os.read(src, 1024 * 1024):
                                if self.control.cancelled():
                                    raise Cancelled("Export cancelled; incomplete destination retained")
                                output.write(block)
                                digest.update(block)
                                count += len(block)
                            output.flush()
                            os.fsync(output.fileno())
                        if (
                            (count, digest.hexdigest()) != (source["size"], source["sha256"])
                            or before != fingerprint(os.fstat(src))
                            or before != root.current_fingerprint(source["path"])
                        ):
                            raise IntegrityError("Archive changed; incomplete export retained for review")
                root.revalidate(self.config.volume_id)
                self.control.guard()
            fd = os.open(
                "manifest.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory
            )
            with os.fdopen(fd, "w") as stream:
                json.dump(receipt, stream, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.fsync(directory)
        except OSError:
            raise StorageError(
                "Export could not complete; preserve any partial destination and retry to a new directory"
            ) from None
        finally:
            if directory is not None:
                os.close(directory)
            os.close(parent)
        return {"archive_id": archive_id, "complete": True}
