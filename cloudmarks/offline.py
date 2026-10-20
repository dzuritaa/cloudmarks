"""Verified individual-file controls. Never removes a cloud/mounted path.

Publication and eviction require the managed view unmounted. Durable intent is
recorded before changing names; restart rolls incomplete operations back. Old
versions/ambiguous candidates remain in private recovery storage and count
against the budget. No automatic garbage collection is performed.
"""

import ctypes
import errno
import hashlib
import json
import os
import stat
import uuid
from dataclasses import asdict, replace

from .model import IntegrityError, LocalCopy, ScopeError, StorageError
from .offline_config import check_volume, require_unmounted
from .onedrive import Cancelled, checksum
from .paths import SecureRoot, relative_parts


def exists(root, path):
    try:
        with root.parent(path) as (parent, name):
            os.stat(name, dir_fd=parent, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


def rename(root, source, target):
    """Linux atomic rename without replacing any unrelated destination."""
    relative_parts(source)
    relative_parts(target)
    with root.parent(source) as (src, name), root.parent(target, create=True) as (dst, dest):
        libc = ctypes.CDLL(None, use_errno=True)
        call = libc.renameat2
        call.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        call.restype = ctypes.c_int
        if call(src, os.fsencode(name), dst, os.fsencode(dest), 1):  # RENAME_NOREPLACE
            value = ctypes.get_errno()
            raise OSError(value, os.strerror(value))
        os.fsync(src)
        os.fsync(dst)


def remove(root, path, size, sha256):
    verified = root.verify(path, size, sha256)
    with root.parent(path) as (parent, name):
        if root.current_fingerprint(path) != verified:
            raise IntegrityError("Local data changed before removal; preserve it")
        os.unlink(name, dir_fd=parent)
        os.fsync(parent)


def usage(root):
    """Explicit control-time audit only; never called from Nautilus/status IPC."""

    def walk(fd):
        total = 0
        for name in os.listdir(fd):
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if info.st_dev != root.device or stat.S_ISLNK(info.st_mode):
                raise ScopeError("Store contains an unexpected link or nested filesystem")
            if stat.S_ISDIR(info.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    total += walk(child)
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                total += max(info.st_size, info.st_blocks * 512)
            else:
                raise ScopeError("Store contains unsupported content; refusing new work")
        return total

    return walk(root.fd)


class OfflineController:
    def __init__(
        self,
        config,
        store,
        backend,
        settings,
        *,
        volume_check=check_volume,
        unmounted=require_unmounted,
        progress=None,
        cancelled=lambda: False,
        hook=lambda phase: None,
    ):
        if config != store.config:
            raise ScopeError("Controller and state binding disagree")
        self.config, self.store, self.backend, self.settings = config, store, backend, settings
        self.volume_check, self.unmounted = volume_check, unmounted
        self.progress = progress or (lambda **values: None)
        self.cancelled, self.hook = cancelled, hook

    def guard(self):
        self.volume_check(self.config, self.settings)
        self.unmounted(self.settings)

    def entry(self, identity):
        row = self.store.connection.execute(
            "SELECT relative_path FROM local_entries WHERE drive_id=? AND item_id=?", self.store.key(identity)
        ).fetchone()
        copy = self.store.copy(identity)
        if row is None:
            if copy:
                raise IntegrityError("A local copy has no managed publication record")
            return None
        if copy is None:
            raise IntegrityError("Managed publication has no completed copy")
        return {"path": row[0], "copy": {k: v for k, v in asdict(copy).items() if k != "identity"}}

    def verify_entry(self, root, entry):
        value = entry["copy"]
        root.verify(value["object_path"], value["size"], value["sha256"])
        root.verify("local/" + entry["path"], value["size"], value["sha256"])

    def journal(self, identity, payload):
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO publications VALUES (?,?,?)", (*self.store.key(identity), json.dumps(payload))
            )

    def recover(self):
        self.guard()
        with SecureRoot(self.config.storage_root, self.config.volume_id) as root:
            rows = self.store.connection.execute("SELECT * FROM publications").fetchall()
            for row in rows:
                payload = json.loads(row["payload"])
                from .model import FileIdentity

                identity = FileIdentity(row["drive_id"], row["item_id"])
                self.config.validate_identity(identity)
                if payload.get("action") == "publish":
                    self.rollback_publication(root, identity, payload)
                elif payload.get("action") == "evict":
                    self.rollback_eviction(root, identity, payload)
                elif payload.get("action") == "archive":
                    from .recovery import rollback_archive

                    rollback_archive(self, root, identity, payload)
                else:
                    raise IntegrityError("Unknown recovery intent; preserve all data")
                with self.store.transaction() as connection:
                    connection.execute(
                        "DELETE FROM publications WHERE drive_id=? AND item_id=?", self.store.key(identity)
                    )
                    connection.execute(
                        "UPDATE files SET operation='error',error=? WHERE drive_id=? AND item_id=?",
                        (
                            "Interrupted local operation recovered; previous data retained",
                            *self.store.key(identity),
                        ),
                    )
            root.revalidate(self.config.volume_id)

    def rollback_publication(self, root, identity, payload):
        old = self.entry(identity)
        if payload["old"] != old:
            raise IntegrityError("Recovery intent disagrees with committed data")
        path, backup = payload["path"], payload["backup"]
        if not path.startswith("local/") or not backup.startswith("recovery/"):
            raise ScopeError("Invalid publication recovery location")
        if exists(root, backup):
            if not old:
                raise IntegrityError("Unexpected recovery snapshot")
            root.verify(backup, old["copy"]["size"], old["copy"]["sha256"])
            if exists(root, path):
                root.verify(path, payload["size"], payload["sha256"])
                rename(root, path, "recovery/" + uuid.uuid4().hex + ".aborted")
            rename(root, backup, path)
        elif old:
            self.verify_entry(root, old)
        elif exists(root, path):
            root.verify(path, payload["size"], payload["sha256"])
            rename(root, path, "recovery/" + uuid.uuid4().hex + ".aborted")

    def rollback_eviction(self, root, identity, payload):
        entry = self.entry(identity)
        if not entry or payload["old"] != entry:
            raise IntegrityError("Eviction recovery disagrees with committed data")
        value = entry["copy"]
        for original, backup in (
            ("local/" + entry["path"], payload["view_backup"]),
            (value["object_path"], payload["object_backup"]),
        ):
            if not backup.startswith("recovery/"):
                raise ScopeError("Invalid eviction recovery location")
            if exists(root, backup):
                root.verify(backup, value["size"], value["sha256"])
                rename(root, backup, original)
            else:
                root.verify(original, value["size"], value["sha256"])

    def space(self, root, needed):
        free = os.fstatvfs(root.fd)
        if free.f_bavail * free.f_frsize < self.settings["min_free_bytes"] + needed:
            raise StorageError("Minimum free-space reserve would be exceeded")
        root.revalidate(self.config.volume_id)

    def keep(self, path, *, pin=True):
        self.recover()
        observation = self.backend.describe(path)
        remote = observation.remote
        self.config.validate_remote(remote)
        if self.store.connection.execute(
            "SELECT 1 FROM issues WHERE drive_id=? AND item_id=?", self.store.key(remote.identity)
        ).fetchone():
            raise ScopeError("Unresolved change requires review; use offline conflicts")
        old = self.entry(remote.identity)
        if old and old["path"] != path:
            raise ScopeError("A published rename requires explicit recovery; preserve the previous path")
        row = self.store.row(remote.identity)
        if row and (row["dirty"] or row["operation"] == "active"):
            raise ScopeError("Modified data or an active transfer prevents replacement")
        self.store.observe(remote)
        if pin:
            self.store.set_pin(remote.identity, True)
        self.store.begin_transfer(remote.identity)
        try:
            with SecureRoot(self.config.storage_root, self.config.volume_id) as root:
                if old:
                    self.verify_entry(root, old)
                elif exists(root, "local/" + path):
                    raise ScopeError("Refusing to replace an unmanaged local file")
                # Two complete independent files: immutable object + visible
                # tree. Include all old/orphan/partial bytes in actual usage.
                reserve = 2 * max(4096, ((remote.size + 4095) // 4096) * 4096) + 65536
                if usage(root) + reserve > self.settings["budget_bytes"]:
                    raise StorageError("Offline storage budget cannot accommodate this file")
                self.space(root, reserve)
                candidate, sha = self.download(root, observation)
                latest = self.backend.describe(path)
                if not observation.same_version(latest):
                    if latest.remote.identity == remote.identity:
                        self.store.observe(latest.remote)
                    raise IntegrityError(
                        "Remote identity, path, checksum or revision changed during download"
                    )
                complete_remote = replace(latest.remote, sha256=sha)
                self.store.observe(complete_remote)
                self.guard()
                root.revalidate(self.config.volume_id)
                published = root.publish(candidate)
                with root.open_regular(published) as fd:
                    os.fchmod(fd, 0o444)
                    os.fsync(fd)
                copy = LocalCopy(remote.identity, remote.revision, published, remote.size, sha)
                self.space(root, remote.size + 65536)
                with root.open_regular(published) as fd:

                    def blocks():
                        while block := os.read(fd, 256 * 1024):
                            self.check_cancelled()
                            self.space(root, len(block))
                            yield block

                    visible = root.stage(blocks(), remote.size, sha)
                self.check_cancelled()
                root.verify(published, remote.size, sha)
                root.verify(visible, remote.size, sha)
                with root.open_regular(visible) as fd:
                    os.fchmod(fd, 0o444)
                    os.fsync(fd)
                final = self.backend.describe(path)
                if not observation.same_version(final):
                    raise IntegrityError("Remote version changed before visible publication")
                self.store.observe(replace(final.remote, sha256=sha))
                payload = {
                    "action": "publish",
                    "path": "local/" + path,
                    "backup": "recovery/" + uuid.uuid4().hex + ".previous",
                    "old": old,
                    "size": remote.size,
                    "sha256": sha,
                }
                self.check_cancelled()
                self.journal(remote.identity, payload)
                self.hook("intent")
                if old:
                    rename(root, "local/" + path, payload["backup"])
                self.hook("backup")
                rename(root, visible, "local/" + path)
                self.hook("visible")
                root.verify("local/" + path, remote.size, sha)
                self.guard()
                root.revalidate(self.config.volume_id)
                with self.store.transaction() as connection:
                    self.store._record_complete(connection, copy)
                    connection.execute(
                        "INSERT INTO local_entries VALUES (?,?,?) ON CONFLICT(drive_id,item_id) DO UPDATE SET relative_path=excluded.relative_path",
                        (*self.store.key(remote.identity), path),
                    )
                    connection.execute(
                        "DELETE FROM publications WHERE drive_id=? AND item_id=?",
                        self.store.key(remote.identity),
                    )
                self.hook("committed")
                self.progress(phase="complete", completed=remote.size, total=remote.size)
                return copy
        except BaseException:
            self.store.fail_transfer(
                remote.identity, "Cancelled or incomplete operation; previous data retained; retry explicitly"
            )
            # Roll back an intent immediately when possible. Abrupt process
            # death leaves the durable record for next startup instead.
            self.recover()
            raise

    def check_cancelled(self):
        if self.cancelled():
            raise Cancelled("Download cancelled; previous data was retained")

    def download(self, root, observation):
        remote = observation.remote
        relative = "objects/.partial-" + uuid.uuid4().hex
        sha, expected = hashlib.sha256(), checksum(observation.kind)
        with root.parent(relative) as (parent, name):
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            try:
                count = 0
                self.progress(phase="download", completed=0, total=remote.size)
                for block in self.backend.chunks(observation, cancelled=self.cancelled):
                    self.check_cancelled()
                    if (
                        not isinstance(block, bytes)
                        or len(block) > 1024 * 1024
                        or count + len(block) > remote.size
                    ):
                        raise IntegrityError("Transport exceeded the expected length/block limit")
                    self.space(root, len(block) + remote.size + 65536)
                    count += len(block)
                    sha.update(block)
                    expected.update(block)
                    rest = memoryview(block)
                    while rest:
                        written = os.write(fd, rest)
                        if written <= 0:
                            raise OSError(errno.ENOSPC, "Download write made no progress")
                        rest = rest[written:]
                    self.progress(phase="download", completed=count, total=remote.size)
                self.check_cancelled()
                if count != remote.size or expected.digest() != observation.digest:
                    raise IntegrityError("Remote checksum or byte length did not match")
                os.fsync(fd)
                os.fsync(parent)
            finally:
                os.close(fd)
        return relative, sha.hexdigest()

    def unpin(self, path):
        self.recover()
        identity = self.identity_at(path)
        row = self.store.row(identity)
        if row["operation"] == "active":
            raise ScopeError("Cannot unpin an active transfer")
        self.store.set_pin(identity, False)

    def identity_at(self, path):
        relative_parts(path)
        row = self.store.connection.execute(
            "SELECT item_id,relative_path FROM files WHERE drive_id=? AND path_key=?",
            (self.config.expected_drive_id, self.config.path_key(path)),
        ).fetchone()
        if not row or row[1] != path:
            raise ScopeError("No exact managed observation for this path")
        from .model import FileIdentity

        return FileIdentity(self.config.expected_drive_id, row[0])

    def free(self, path):
        self.recover()
        identity = self.identity_at(path)
        row, entry = self.store.row(identity), self.entry(identity)
        if self.store.connection.execute(
            "SELECT 1 FROM issues WHERE drive_id=? AND item_id=?", self.store.key(identity)
        ).fetchone():
            raise ScopeError("Unresolved change prevents eviction; preserve all versions")
        if not entry or row["dirty"] or row["pinned"] or row["operation"] != "idle":
            raise ScopeError("Unpin first; only a clean, idle completed copy may be evicted")
        observation = self.backend.describe(path)  # Must succeed online, never infer absence/deletion.
        remote, value = observation.remote, entry["copy"]
        if remote.deleted or remote.is_directory:
            raise ScopeError("Deleted items and directories are never eviction evidence")
        if remote.identity != identity or (remote.revision, remote.size) != (
            value["revision"],
            value["size"],
        ):
            raise IntegrityError("Fresh remote version does not match the local copy; preserve it")
        self.config.validate_remote(remote)
        with SecureRoot(self.config.storage_root, self.config.volume_id) as root:
            self.verify_entry(root, entry)
            check = checksum(observation.kind)
            with root.open_regular(value["object_path"]) as fd:
                while block := os.read(fd, 256 * 1024):
                    self.check_cancelled()
                    check.update(block)
            if check.digest() != observation.digest:
                raise IntegrityError("Remote checksum differs; eviction refused")
            # Recheck the remote immediately before local mutation, not just
            # before potentially long integrity work.
            latest = self.backend.describe(path)
            if not observation.same_version(latest):
                raise IntegrityError("Remote changed while checking eviction")
            self.check_cancelled()
            self.store.observe(replace(latest.remote, sha256=value["sha256"]))
            self.guard()
            root.revalidate(self.config.volume_id)
            payload = {
                "action": "evict",
                "old": entry,
                "view_backup": "recovery/" + uuid.uuid4().hex + ".evict-view",
                "object_backup": "recovery/" + uuid.uuid4().hex + ".evict-object",
            }
            self.journal(identity, payload)
            try:
                rename(root, "local/" + path, payload["view_backup"])
                self.hook("evict-visible")
                rename(root, value["object_path"], payload["object_backup"])
                self.hook("evict-object")
                root.verify(payload["view_backup"], value["size"], value["sha256"])
                root.verify(payload["object_backup"], value["size"], value["sha256"])
                with self.store.transaction() as connection:
                    connection.execute(
                        "DELETE FROM local_entries WHERE drive_id=? AND item_id=?", self.store.key(identity)
                    )
                    connection.execute(
                        "DELETE FROM copies WHERE drive_id=? AND item_id=?", self.store.key(identity)
                    )
                    connection.execute(
                        "DELETE FROM publications WHERE drive_id=? AND item_id=?", self.store.key(identity)
                    )
                for backup in (payload["view_backup"], payload["object_backup"]):
                    remove(root, backup, value["size"], value["sha256"])
            except BaseException:
                self.recover()
                raise
        self.progress(phase="freed", completed=value["size"] * 2, total=value["size"] * 2)
