"""Single-writer, transactional metadata. Filesystem publication is separate."""

import fcntl
import json
import os
import sqlite3
import stat
from contextlib import contextmanager, nullcontext
from dataclasses import asdict
from pathlib import Path
from urllib.parse import quote

from .model import ConfigurationError, LocalCopy, RemoteFile, ScopeError, StorageError
from .paths import open_absolute_directory

SCHEMA_VERSION = 4


class StateStore:
    def __init__(self, config):
        self.config = config
        self.connection = None
        self.lock = None
        self._directory_fd = None
        path = Path(config.state_path)
        try:
            # The caller creates its private state directory. Do not traverse
            # symlinks or silently create an arbitrary user-supplied path tree.
            self._directory_fd = open_absolute_directory(path.parent)
            parent = os.fstat(self._directory_fd)
            if parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) & 0o077:
                raise StorageError("State directory must be owned by this user and private (0700)")
            self.lock = os.open(
                path.name + ".lock",
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=self._directory_fd,
            )
            lock_info = os.fstat(self.lock)
            if (
                not stat.S_ISREG(lock_info.st_mode)
                or lock_info.st_nlink != 1
                or lock_info.st_uid != os.getuid()
            ):
                raise StorageError("State lock must be a regular, singly linked file owned by this user")
            os.fchmod(self.lock, 0o600)
            try:
                fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise StorageError("Another CloudMarks state writer is already running") from None
            fd = os.open(
                path.name,
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=self._directory_fd,
            )
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
                    raise StorageError("State file must be a regular file owned by this user")
                os.fchmod(fd, 0o600)
            finally:
                os.close(fd)
            # Open through the pinned parent FD, rather than re-resolving its
            # potentially changed ancestor pathname between validation and open.
            self.connection = sqlite3.connect(
                f"/proc/self/fd/{self._directory_fd}/{path.name}", isolation_level=None
            )
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA foreign_keys=ON")
            self.connection.execute("PRAGMA synchronous=FULL")
            self.connection.execute("PRAGMA journal_mode=DELETE")
            version = self.connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2, 3, SCHEMA_VERSION):
                raise ConfigurationError("Unsupported state schema; explicit migration is required")
            if version == 0:
                self._create_schema()
            binding = self.connection.execute(
                "SELECT drive_id, root_id, volume_id, storage_root, case_sensitive FROM binding"
            ).fetchone()
            expected = (
                config.expected_drive_id,
                config.allowed_root_id,
                config.volume_id,
                str(config.storage_root),
                int(config.case_sensitive),
            )
            if binding is None:
                self.connection.execute("INSERT INTO binding VALUES (?, ?, ?, ?, ?)", expected)
            elif tuple(binding) != expected:
                raise ScopeError("State belongs to a different account, root, disk or store path")
            if version in (0, 1):
                self.connection.executescript("""
                    BEGIN IMMEDIATE;
                    CREATE TABLE IF NOT EXISTS local_entries (
                        drive_id TEXT NOT NULL, item_id TEXT NOT NULL, relative_path TEXT NOT NULL,
                        PRIMARY KEY(drive_id,item_id), UNIQUE(relative_path),
                        FOREIGN KEY(drive_id,item_id) REFERENCES copies(drive_id,item_id)
                    );
                    CREATE TABLE IF NOT EXISTS publications (
                        drive_id TEXT NOT NULL, item_id TEXT NOT NULL, payload TEXT NOT NULL,
                        PRIMARY KEY(drive_id,item_id)
                    );
                    PRAGMA user_version=2;
                    COMMIT;
                """)
            if version in (0, 1, 2):
                self.connection.executescript("""
                    BEGIN IMMEDIATE;
                    CREATE TABLE IF NOT EXISTS jobs (
                        drive_id TEXT NOT NULL, item_id TEXT NOT NULL,
                        requested_path TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                        attempts INTEGER NOT NULL DEFAULT 0, error TEXT,
                        PRIMARY KEY(drive_id,item_id),
                        FOREIGN KEY(drive_id,item_id) REFERENCES files(drive_id,item_id)
                    );
                    CREATE TABLE IF NOT EXISTS changes (
                        sequence INTEGER PRIMARY KEY, drive_id TEXT NOT NULL, item_id TEXT NOT NULL,
                        kind TEXT NOT NULL, previous TEXT NOT NULL, current TEXT NOT NULL,
                        observed_at REAL NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS issues (
                        drive_id TEXT NOT NULL, item_id TEXT NOT NULL, reason TEXT NOT NULL,
                        revision TEXT NOT NULL, relative_path TEXT NOT NULL, candidate TEXT,
                        PRIMARY KEY(drive_id,item_id),
                        FOREIGN KEY(drive_id,item_id) REFERENCES files(drive_id,item_id)
                    );
                    CREATE TABLE IF NOT EXISTS folder_intents (
                        drive_id TEXT NOT NULL, item_id TEXT NOT NULL, manifest TEXT NOT NULL,
                        scanned_at REAL NOT NULL, pinned INTEGER NOT NULL DEFAULT 1,
                        PRIMARY KEY(drive_id,item_id),
                        FOREIGN KEY(drive_id,item_id) REFERENCES files(drive_id,item_id)
                    );
                    CREATE TABLE IF NOT EXISTS pin_sources (
                        drive_id TEXT NOT NULL, item_id TEXT NOT NULL, owner TEXT NOT NULL,
                        PRIMARY KEY(drive_id,item_id,owner),
                        FOREIGN KEY(drive_id,item_id) REFERENCES files(drive_id,item_id)
                    );
                    INSERT OR IGNORE INTO pin_sources SELECT drive_id,item_id,'direct' FROM files WHERE pinned=1;
                    CREATE TABLE IF NOT EXISTS snapshots (
                        object_path TEXT PRIMARY KEY, drive_id TEXT NOT NULL, item_id TEXT NOT NULL,
                        revision TEXT NOT NULL, relative_path TEXT NOT NULL,
                        size INTEGER NOT NULL, sha256 TEXT NOT NULL, created_at REAL NOT NULL
                    );
                    PRAGMA user_version=3;
                    COMMIT;
                """)
            if version in (0, 1, 2, 3):
                self.connection.executescript("""
                    BEGIN IMMEDIATE;
                    CREATE TABLE IF NOT EXISTS archives (
                        archive_id TEXT PRIMARY KEY, payload TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS verifications (
                        drive_id TEXT NOT NULL, item_id TEXT NOT NULL, payload TEXT NOT NULL,
                        PRIMARY KEY(drive_id,item_id)
                    );
                    PRAGMA user_version=4;
                    COMMIT;
                """)
            # A durable job is retried explicitly, never from the display helper.
            self.connection.execute(
                "UPDATE jobs SET status='pending', error='Worker interrupted; resume explicitly' WHERE status='running'"
            )
            # An operation from a dead process cannot still be 'transferring'.
            self.connection.execute(
                "UPDATE files SET operation='error', error='Transfer interrupted by restart' WHERE operation='active'"
            )
        except BaseException:
            self.close()
            raise

    def _create_schema(self):
        self.connection.executescript("""
            BEGIN IMMEDIATE;
            CREATE TABLE binding (
                drive_id TEXT NOT NULL, root_id TEXT NOT NULL,
                volume_id TEXT NOT NULL, storage_root TEXT NOT NULL, case_sensitive INTEGER NOT NULL
            );
            CREATE TABLE files (
                drive_id TEXT NOT NULL, item_id TEXT NOT NULL,
                root_id TEXT NOT NULL, relative_path TEXT NOT NULL, path_key TEXT NOT NULL,
                revision TEXT NOT NULL, size INTEGER NOT NULL CHECK(size >= 0),
                observed_at REAL NOT NULL, sha256 TEXT, deleted INTEGER NOT NULL DEFAULT 0,
                is_directory INTEGER NOT NULL DEFAULT 0,
                pinned INTEGER NOT NULL DEFAULT 0, dirty INTEGER NOT NULL DEFAULT 0,
                operation TEXT NOT NULL DEFAULT 'idle', error TEXT,
                PRIMARY KEY(drive_id, item_id), UNIQUE(drive_id, path_key)
            );
            CREATE TABLE copies (
                drive_id TEXT NOT NULL, item_id TEXT NOT NULL, revision TEXT NOT NULL,
                object_path TEXT NOT NULL, size INTEGER NOT NULL, sha256 TEXT NOT NULL,
                PRIMARY KEY(drive_id, item_id),
                FOREIGN KEY(drive_id, item_id) REFERENCES files(drive_id, item_id)
            );
            PRAGMA user_version=1;
            COMMIT;
        """)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None
        if self.lock is not None:
            os.close(self.lock)
            self.lock = None
        if self._directory_fd is not None:
            os.close(self._directory_fd)
            self._directory_fd = None

    @contextmanager
    def transaction(self):
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield self.connection
            self.connection.execute("COMMIT")
        except BaseException:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise

    @staticmethod
    def key(identity):
        return identity.drive_id, identity.item_id

    def observe(self, remote, *, connection=None):
        self.config.validate_remote(remote)
        key = self.key(remote.identity)
        previous = self.remote(remote.identity)
        with self.transaction() if connection is None else nullcontext(connection) as connection:
            existing = connection.execute(
                "SELECT observed_at, revision FROM files WHERE drive_id=? AND item_id=?", key
            ).fetchone()
            if existing and existing[0] > remote.observed_at:
                raise ScopeError("Refusing an older observation that could roll state backward")
            if existing and existing[0] == remote.observed_at and existing[1] != remote.revision:
                raise ScopeError("Competing revisions have no ordered observation evidence")
            connection.execute(
                """
                INSERT INTO files (drive_id, item_id, root_id, relative_path, path_key,
                                   revision, size, observed_at, sha256, deleted, is_directory)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(drive_id, item_id) DO UPDATE SET
                    root_id=excluded.root_id, relative_path=excluded.relative_path,
                    path_key=excluded.path_key, revision=excluded.revision,
                    size=excluded.size, observed_at=excluded.observed_at,
                    sha256=excluded.sha256, deleted=excluded.deleted, is_directory=excluded.is_directory
            """,
                (
                    *key,
                    remote.root_id,
                    remote.relative_path,
                    self.config.path_key(remote.relative_path),
                    remote.revision,
                    remote.size,
                    remote.observed_at,
                    remote.sha256,
                    int(remote.deleted),
                    int(remote.is_directory),
                ),
            )

            if previous and (
                previous.relative_path,
                previous.revision,
                previous.size,
                previous.deleted,
                previous.is_directory,
            ) != (remote.relative_path, remote.revision, remote.size, remote.deleted, remote.is_directory):
                kind = (
                    "unavailable"
                    if remote.deleted
                    else ("rename" if previous.relative_path != remote.relative_path else "revision")
                )
                connection.execute(
                    "INSERT INTO changes(drive_id,item_id,kind,previous,current,observed_at) VALUES(?,?,?,?,?,?)",
                    (
                        *key,
                        kind,
                        json.dumps(asdict(previous)),
                        json.dumps(asdict(remote)),
                        remote.observed_at,
                    ),
                )

    def row(self, identity):
        self.config.validate_identity(identity)
        return self.connection.execute(
            "SELECT * FROM files WHERE drive_id=? AND item_id=?", self.key(identity)
        ).fetchone()

    def remote(self, identity):
        row = self.row(identity)
        if row is None:
            return None
        remote = RemoteFile(
            identity,
            row["root_id"],
            row["relative_path"],
            row["revision"],
            row["size"],
            row["observed_at"],
            row["sha256"],
            bool(row["deleted"]),
            bool(row["is_directory"]),
        )
        self.config.validate_remote(remote)
        return remote

    def copy(self, identity):
        self.config.validate_identity(identity)
        row = self.connection.execute(
            "SELECT * FROM copies WHERE drive_id=? AND item_id=?", self.key(identity)
        ).fetchone()
        return (
            LocalCopy(identity, row["revision"], row["object_path"], row["size"], row["sha256"])
            if row
            else None
        )

    def set_pin(self, identity, pinned):
        if not isinstance(pinned, bool):
            raise ConfigurationError("Pin intent must be a boolean")
        self.config.validate_identity(identity)
        with self.transaction() as connection:
            if self.row(identity) is None:
                raise ScopeError("File has no observation in this store")
            if pinned:
                connection.execute(
                    "INSERT OR IGNORE INTO pin_sources VALUES(?,?, 'direct')", self.key(identity)
                )
            else:
                connection.execute(
                    "DELETE FROM pin_sources WHERE drive_id=? AND item_id=? AND owner='direct'",
                    self.key(identity),
                )
            self.recompute_pin(connection, identity)

    def recompute_pin(self, connection, identity):
        connection.execute(
            "UPDATE files SET pinned=EXISTS(SELECT 1 FROM pin_sources WHERE drive_id=? AND item_id=?) "
            "WHERE drive_id=? AND item_id=?",
            (*self.key(identity), *self.key(identity)),
        )
        connection.execute(
            "UPDATE jobs SET status='cancelled',error=NULL WHERE drive_id=? AND item_id=? "
            "AND NOT EXISTS(SELECT 1 FROM pin_sources WHERE drive_id=? AND item_id=?)",
            (*self.key(identity), *self.key(identity)),
        )

    def set_dirty(self, identity, dirty):
        if not isinstance(dirty, bool):
            raise ConfigurationError("Dirty state must be a boolean")
        self._update(identity, "dirty=?", (int(dirty),))

    def _update(self, identity, assignments, values):
        # assignments are internal constants, never caller-supplied SQL.
        self.config.validate_identity(identity)
        cursor = self.connection.execute(
            f"UPDATE files SET {assignments} WHERE drive_id=? AND item_id=?", (*values, *self.key(identity))
        )
        if cursor.rowcount != 1:
            raise ScopeError("File has no observation in this store")

    def begin_transfer(self, identity):
        remote = self.remote(identity)
        row = self.row(identity)
        if remote is None or remote.deleted or remote.is_directory or row["dirty"]:
            raise ScopeError("File cannot start a transfer in its current state")
        if row["operation"] == "active":
            raise ScopeError("A transfer is already active for this file")
        self._update(identity, "operation='active', error=NULL", ())

    def fail_transfer(self, identity, reason):
        # Stable public reason, rather than raw adapter errors with private paths.
        self._update(identity, "operation='error', error=?", (reason[:256],))

    def record_complete(self, copy):
        # Caller verifies bytes and the supported open path separately. This row
        # is evidence only; StatusEngine never trusts it without a fresh check.
        self.config.validate_identity(copy.identity)
        with self.transaction() as connection:
            self._record_complete(connection, copy)

    def _record_complete(self, connection, copy):
        row = self.row(copy.identity)
        if (
            row is None
            or row["deleted"]
            or row["is_directory"]
            or row["dirty"]
            or row["revision"] != copy.revision
            or row["size"] != copy.size
            or row["sha256"] != copy.sha256
        ):
            raise ScopeError("Completion no longer matches the observed remote revision")
        if row["operation"] != "active":
            raise ScopeError("Completion has no active tracked operation")
        connection.execute(
            """INSERT INTO copies VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(drive_id, item_id) DO UPDATE SET revision=excluded.revision,
                object_path=excluded.object_path, size=excluded.size, sha256=excluded.sha256""",
            (*self.key(copy.identity), copy.revision, copy.object_path, copy.size, copy.sha256),
        )
        connection.execute(
            "UPDATE files SET operation='idle', error=NULL WHERE drive_id=? AND item_id=?",
            self.key(copy.identity),
        )


class ReadStore(StateStore):
    """Independent SQLite read-only connection for the display-only helper.

    No writer lock, recovery, schema migration, pragma mutation or chmod. A
    control process may update the DB; StatusEngine rechecks its evidence.
    """

    def __init__(self, config):
        self.config = config
        self.connection = self.lock = self._directory_fd = None
        try:
            path = Path(config.state_path)
            self._directory_fd = open_absolute_directory(path.parent)
            parent = os.fstat(self._directory_fd)
            if parent.st_uid != os.getuid() or parent.st_mode & 0o077:
                raise StorageError("State directory must be private and user-owned")
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self._directory_fd)
            try:
                info = os.fstat(fd)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1
                    or info.st_uid != os.getuid()
                    or info.st_mode & 0o077
                ):
                    raise StorageError("State file must be private, singly linked and user-owned")
            finally:
                os.close(fd)
            self.connection = sqlite3.connect(
                f"file:/proc/self/fd/{self._directory_fd}/{quote(path.name, safe='')}?mode=ro",
                uri=True,
                isolation_level=None,
                timeout=0.1,
            )
            self.connection.row_factory = sqlite3.Row
            if self.connection.execute("PRAGMA user_version").fetchone()[0] not in (1, 2, 3, SCHEMA_VERSION):
                raise ConfigurationError("Unsupported read-only state schema")
            binding = self.connection.execute(
                "SELECT drive_id,root_id,volume_id,storage_root,case_sensitive FROM binding"
            ).fetchone()
            if binding is None or tuple(binding) != (
                config.expected_drive_id,
                config.allowed_root_id,
                config.volume_id,
                str(config.storage_root),
                int(config.case_sensitive),
            ):
                raise ScopeError("State belongs to a different account/root/disk")
        except BaseException:
            self.close()
            raise
