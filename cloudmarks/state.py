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

SCHEMA_VERSION = 6


class StateStore:
    def __init__(self, config):
        self.config = config
        # None only while constructing or after close().
        self.connection: sqlite3.Connection = None  # type: ignore[assignment]
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
            self.connection.create_function("cm_path_key", 1, config.path_key, deterministic=True)
            self.connection.execute("PRAGMA foreign_keys=ON")
            self.connection.execute("PRAGMA synchronous=FULL")
            self.connection.execute("PRAGMA journal_mode=DELETE")
            version = self.connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2, 3, 4, 5, SCHEMA_VERSION):
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
            if version in (0, 1, 2, 3, 4):
                self.connection.executescript("""
                    BEGIN IMMEDIATE;
                    CREATE UNIQUE INDEX IF NOT EXISTS local_entries_path_key
                        ON local_entries(cm_path_key(relative_path));
                    CREATE INDEX IF NOT EXISTS pin_sources_owner ON pin_sources(owner);
                    PRAGMA user_version=5;
                    COMMIT;
                """)
            if version in (0, 1, 2, 3, 4, 5):
                # Durable selection intent for an in-progress Free. Pins are
                # released before local copies are evicted; this row restores
                # them if the process dies before the release completes.
                self.connection.executescript("""
                    BEGIN IMMEDIATE;
                    CREATE TABLE IF NOT EXISTS releases (
                        drive_id TEXT NOT NULL, item_id TEXT NOT NULL, payload TEXT NOT NULL,
                        PRIMARY KEY(drive_id,item_id)
                    );
                    PRAGMA user_version=6;
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
            # The writer lock is exclusive, so a release row here belongs to a
            # Free that died before finishing. Restore the user's selection.
            self.restore_releases()
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
            self.connection = None  # type: ignore[assignment]
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

    # Read-only queries shared by the status engine, helper and observers.
    # Callers gate on schema_version() where older schemas lack a table.

    def schema_version(self):
        return self.connection.execute("PRAGMA user_version").fetchone()[0]

    def data_version(self):
        return self.connection.execute("PRAGMA data_version").fetchone()[0]

    def file_at(self, path):
        """Tracked item at a path key: (item_id, size) or None."""
        return self.connection.execute(
            "SELECT item_id,size FROM files WHERE drive_id=? AND path_key=?",
            (self.config.expected_drive_id, self.config.path_key(path)),
        ).fetchone()

    def issue(self, identity):
        return self.connection.execute(
            "SELECT * FROM issues WHERE drive_id=? AND item_id=?", self.key(identity)
        ).fetchone()

    def folder_intent(self, identity):
        return self.connection.execute(
            "SELECT * FROM folder_intents WHERE drive_id=? AND item_id=?", self.key(identity)
        ).fetchone()

    def verification_payload(self, identity):
        row = self.connection.execute(
            "SELECT payload FROM verifications WHERE drive_id=? AND item_id=?", self.key(identity)
        ).fetchone()
        return None if row is None else row[0]

    def local_entry(self, identity):
        return self.connection.execute(
            "SELECT relative_path FROM local_entries WHERE drive_id=? AND item_id=?", self.key(identity)
        ).fetchone()

    def local_entry_at(self, path):
        """Retained publication occupying a path, matched by path key from schema 5."""
        if self.schema_version() >= 5:
            return self.connection.execute(
                "SELECT item_id,relative_path FROM local_entries WHERE cm_path_key(relative_path)=?",
                (self.config.path_key(path),),
            ).fetchone()
        return self.connection.execute(
            "SELECT item_id,relative_path FROM local_entries WHERE relative_path=?", (path,)
        ).fetchone()

    def local_entries_under(self, path):
        return self.connection.execute(
            "SELECT item_id,relative_path FROM local_entries WHERE substr(relative_path,1,?)=?",
            (len(path) + 1, path + "/"),
        ).fetchall()

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

    def check_integrity(self):
        """Read-only consistency check. Returns sorted problem names; empty means consistent.

        Covers SQLite structure and foreign keys plus CloudMarks invariants
        that every writer maintains. Safe on a ReadStore while a writer runs:
        interrupted-operation records are reported, not repaired.
        """
        version = self.schema_version()
        connection = self.connection
        problems = set()
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            problems.add("sqlite_structure")
        if connection.execute("PRAGMA foreign_key_check").fetchone():
            problems.add("foreign_keys")
        checks = {
            "copy_of_directory": (
                1,
                "SELECT 1 FROM copies c JOIN files f ON f.drive_id=c.drive_id AND f.item_id=c.item_id "
                "WHERE f.is_directory=1 LIMIT 1",
            ),
            "copy_without_publication": (
                2,
                "SELECT 1 FROM copies c WHERE NOT EXISTS (SELECT 1 FROM local_entries l "
                "WHERE l.drive_id=c.drive_id AND l.item_id=c.item_id) LIMIT 1",
            ),
            "publication_without_copy": (
                2,
                "SELECT 1 FROM local_entries l WHERE NOT EXISTS (SELECT 1 FROM copies c "
                "WHERE c.drive_id=l.drive_id AND c.item_id=l.item_id) LIMIT 1",
            ),
            "interrupted_operation": (2, "SELECT 1 FROM publications LIMIT 1"),
            "pin_flag_mismatch": (
                3,
                "SELECT 1 FROM files f WHERE f.pinned != EXISTS(SELECT 1 FROM pin_sources p "
                "WHERE p.drive_id=f.drive_id AND p.item_id=f.item_id) LIMIT 1",
            ),
            "unfinished_release": (6, "SELECT 1 FROM releases LIMIT 1"),
        }
        for name, (since, query) in checks.items():
            if version >= since and connection.execute(query).fetchone():
                problems.add(name)
        return sorted(problems)

    def pin_sources_owned(self, owner):
        return self.connection.execute(
            "SELECT drive_id,item_id,owner FROM pin_sources WHERE owner=?", (owner,)
        ).fetchall()

    def direct_pins(self, identity):
        return self.connection.execute(
            "SELECT drive_id,item_id,owner FROM pin_sources WHERE drive_id=? AND item_id=? AND owner='direct'",
            self.key(identity),
        ).fetchall()

    JOB_STATES = frozenset({"pending", "running", "done", "error", "blocked", "cancelled"})

    def set_job(self, identity, status, error=None):
        """Move a durable job to ``status``. Starting a run counts an attempt.

        ``error`` must be a fixed public message: it is persisted and shown.
        """
        if status not in self.JOB_STATES:
            raise ConfigurationError("Unknown job state")
        attempt = ",attempts=attempts+1" if status == "running" else ""
        self.connection.execute(
            f"UPDATE jobs SET status=?,error=?{attempt} WHERE drive_id=? AND item_id=?",
            (status, None if error is None else error[:256], *self.key(identity)),
        )

    def resumable_jobs(self, limit):
        return self.connection.execute(
            "SELECT * FROM jobs WHERE status IN ('pending','error') ORDER BY rowid LIMIT ?", (limit,)
        ).fetchall()

    def finish_job(self, identity):
        self.set_job(identity, "done")

    def begin_release(self, identity, pins, intent):
        """Record the pins a Free is about to release, before releasing any of them.

        ``intent`` is the folder intent's previous ``pinned`` value, or None
        for a single file. Committed separately so the record survives any
        later interruption.
        """
        self.config.validate_identity(identity)
        payload = {"version": 1, "pins": [list(row) for row in pins], "intent": intent}
        with self.transaction() as connection:
            if connection.execute(
                "SELECT 1 FROM releases WHERE drive_id=? AND item_id=?", self.key(identity)
            ).fetchone():
                raise ScopeError("An unfinished release for this selection needs recovery first")
            connection.execute(
                "INSERT INTO releases VALUES (?,?,?)", (*self.key(identity), json.dumps(payload))
            )

    def finish_release(self, identity):
        with self.transaction() as connection:
            connection.execute("DELETE FROM releases WHERE drive_id=? AND item_id=?", self.key(identity))

    def pending_releases(self):
        if self.schema_version() < 6:
            return []
        return self.connection.execute("SELECT drive_id,item_id FROM releases").fetchall()

    def restore_releases(self, identity=None):
        """Put back the pins and folder intent recorded by unfinished releases.

        Idempotent: pins are re-added with INSERT OR IGNORE, and only for
        items that still have an observation. Returns the number restored.
        """
        from .model import FileIdentity

        query = "SELECT drive_id,item_id,payload FROM releases"
        arguments = ()
        if identity is not None:
            query += " WHERE drive_id=? AND item_id=?"
            arguments = self.key(identity)
        with self.transaction() as connection:
            rows = connection.execute(query, arguments).fetchall()
            for row in rows:
                payload = json.loads(row["payload"])
                if payload.get("version") != 1:
                    raise StorageError("Unknown release record; preserve state and review")
                for drive_id, item_id, owner in payload["pins"]:
                    connection.execute(
                        "INSERT OR IGNORE INTO pin_sources SELECT ?,?,? "
                        "WHERE EXISTS(SELECT 1 FROM files WHERE drive_id=? AND item_id=?)",
                        (drive_id, item_id, owner, drive_id, item_id),
                    )
                    self.recompute_pin(connection, FileIdentity(drive_id, item_id))
                if payload["intent"] is not None:
                    connection.execute(
                        "UPDATE folder_intents SET pinned=? WHERE drive_id=? AND item_id=?",
                        (payload["intent"], row["drive_id"], row["item_id"]),
                    )
                connection.execute(
                    "DELETE FROM releases WHERE drive_id=? AND item_id=?", (row["drive_id"], row["item_id"])
                )
        return len(rows)

    # Filesystem intents. A row in ``publications`` means a rename sequence for
    # that item may be half done; recovery rolls it back before new work.

    def begin_intent(self, identity, payload):
        self.config.validate_identity(identity)
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO publications VALUES (?,?,?)", (*self.key(identity), json.dumps(payload))
            )

    def pending_intents(self):
        """(identity, payload) for every unfinished filesystem intent."""
        from .model import FileIdentity

        rows = self.connection.execute("SELECT drive_id,item_id,payload FROM publications").fetchall()
        return [(FileIdentity(row[0], row[1]), json.loads(row[2])) for row in rows]

    def finish_recovered_intent(self, identity):
        """Close an intent that recovery rolled back; the file needs attention."""
        with self.transaction() as connection:
            connection.execute("DELETE FROM publications WHERE drive_id=? AND item_id=?", self.key(identity))
            connection.execute(
                "UPDATE files SET operation='error',error=? WHERE drive_id=? AND item_id=?",
                ("Interrupted local operation recovered; previous data retained", *self.key(identity)),
            )

    def commit_publication(self, copy, relative_path):
        """Atomically record a verified copy, its visible path, and close its intent."""
        self.config.validate_identity(copy.identity)
        with self.transaction() as connection:
            self._record_complete(connection, copy)
            connection.execute(
                "INSERT INTO local_entries VALUES (?,?,?) "
                "ON CONFLICT(drive_id,item_id) DO UPDATE SET relative_path=excluded.relative_path",
                (*self.key(copy.identity), relative_path),
            )
            connection.execute(
                "DELETE FROM publications WHERE drive_id=? AND item_id=?", self.key(copy.identity)
            )

    def commit_eviction(self, identity):
        """Atomically forget a freed copy and close its intent."""
        self.config.validate_identity(identity)
        with self.transaction() as connection:
            for table in ("local_entries", "copies", "publications"):
                connection.execute(f"DELETE FROM {table} WHERE drive_id=? AND item_id=?", self.key(identity))

    def exact_identity_at(self, path):
        """The observed item whose recorded path is exactly ``path``, or None."""
        from .model import FileIdentity

        row = self.connection.execute(
            "SELECT item_id,relative_path FROM files WHERE drive_id=? AND path_key=?",
            (self.config.expected_drive_id, self.config.path_key(path)),
        ).fetchone()
        if not row or row[1] != path:
            return None
        return FileIdentity(self.config.expected_drive_id, row[0])

    # Change tracking (tracking.ChangeTracker).

    REPORTS = {
        "jobs": "SELECT requested_path,status,attempts,error FROM jobs ORDER BY rowid",
        "snapshots": "SELECT relative_path,revision,object_path,size,sha256,created_at FROM snapshots "
        "ORDER BY created_at",
        "changes": "SELECT kind,previous,current,observed_at FROM changes ORDER BY sequence",
        "conflicts": "SELECT files.relative_path,issues.reason,issues.revision,issues.candidate,"
        "local_entries.relative_path AS retained_path,copies.object_path AS original_object "
        "FROM issues JOIN files USING(drive_id,item_id) LEFT JOIN local_entries USING(drive_id,item_id) "
        "LEFT JOIN copies USING(drive_id,item_id) ORDER BY files.relative_path",
    }

    def report(self, name):
        """A read-only review listing: jobs, snapshots, changes or conflicts."""
        return [dict(row) for row in self.connection.execute(self.REPORTS[name])]

    def retained_identities(self):
        from .model import FileIdentity

        return [
            FileIdentity(*row)
            for row in self.connection.execute("SELECT drive_id,item_id FROM local_entries")
        ]

    def retained_identity_at(self, path):
        """The item whose retained publication is exactly ``path`` (it may have been renamed in the cloud)."""
        from .model import FileIdentity

        row = self.connection.execute(
            "SELECT drive_id,item_id FROM local_entries WHERE relative_path=?", (path,)
        ).fetchone()
        return None if row is None else FileIdentity(*row)

    def record_issue(self, remote, reason, candidate=None):
        """Create or replace the conflict for an item; ``candidate`` is a LocalCopy dict or None."""
        self.connection.execute(
            "INSERT INTO issues VALUES(?,?,?,?,?,?) ON CONFLICT(drive_id,item_id) DO UPDATE SET "
            "reason=excluded.reason,revision=excluded.revision,relative_path=excluded.relative_path,"
            "candidate=excluded.candidate",
            (
                *self.key(remote.identity),
                reason,
                remote.revision,
                remote.relative_path,
                json.dumps(candidate) if candidate else None,
            ),
        )

    def enqueue_job(self, remote):
        """Queue (or re-queue) a download of an observed file."""
        self.connection.execute(
            "INSERT INTO jobs(drive_id,item_id,requested_path) VALUES(?,?,?) "
            "ON CONFLICT(drive_id,item_id) DO UPDATE SET status='pending',error=NULL",
            (*self.key(remote.identity), remote.relative_path),
        )

    def record_snapshot(self, object_path, remote, sha256):
        import time

        self.connection.execute(
            "INSERT INTO snapshots VALUES(?,?,?,?,?,?,?,?)",
            (
                object_path,
                *self.key(remote.identity),
                remote.revision,
                remote.relative_path,
                remote.size,
                sha256,
                time.time(),
            ),
        )

    def replace_folder_intent(self, folder, manifest, members):
        """Make ``members`` (observed RemoteFiles) the folder's complete selection, in one transaction.

        Observes every member, replaces the folder's pin sources, records its
        manifest and queues each file. Former members keep any other pins.
        """
        import time

        from .model import FileIdentity

        owner = "folder:" + folder.item_id
        with self.transaction() as connection:
            for remote in members:
                self.observe(remote, connection=connection)
            old_members = connection.execute(
                "SELECT drive_id,item_id FROM pin_sources WHERE owner=?", (owner,)
            ).fetchall()
            connection.execute("DELETE FROM pin_sources WHERE owner=?", (owner,))
            connection.execute(
                "INSERT INTO folder_intents VALUES(?,?,?,?,1) ON CONFLICT(drive_id,item_id) DO UPDATE SET "
                "manifest=excluded.manifest,scanned_at=excluded.scanned_at,pinned=1",
                (*self.key(folder), json.dumps(manifest), time.time()),
            )
            for remote in members:
                connection.execute(
                    "INSERT OR IGNORE INTO pin_sources VALUES(?,?,?)", (*self.key(remote.identity), owner)
                )
                self.recompute_pin(connection, remote.identity)
                if not remote.is_directory:
                    self.enqueue_job(remote)
            for member in old_members:
                self.recompute_pin(connection, FileIdentity(*member))

    def release_folder_intent(self, folder):
        """Unpin a folder selection; members keep any other pins."""
        from .model import FileIdentity

        owner = "folder:" + folder.item_id
        with self.transaction() as connection:
            cursor = connection.execute(
                "UPDATE folder_intents SET pinned=0 WHERE drive_id=? AND item_id=?", self.key(folder)
            )
            if cursor.rowcount != 1:
                raise ScopeError("Folder has no recursive intent")
            members = connection.execute(
                "SELECT drive_id,item_id FROM pin_sources WHERE owner=?", (owner,)
            ).fetchall()
            connection.execute("DELETE FROM pin_sources WHERE owner=?", (owner,))
            for member in members:
                self.recompute_pin(connection, FileIdentity(*member))

    # Conflict archives (recovery.ConflictRecovery).

    def archive_payloads(self):
        return [row[0] for row in self.connection.execute("SELECT payload FROM archives ORDER BY archive_id")]

    def archive(self, archive_id):
        row = self.connection.execute(
            "SELECT payload FROM archives WHERE archive_id=?", (archive_id,)
        ).fetchone()
        return None if row is None else json.loads(row[0])

    def pin_owners(self, identity):
        return [
            row[0]
            for row in self.connection.execute(
                "SELECT owner FROM pin_sources WHERE drive_id=? AND item_id=?", self.key(identity)
            )
        ]

    def commit_archive(self, identity, archive_id, receipt, *, retry=None):
        """Store a conflict receipt and retire the item's local records atomically.

        With ``retry`` (a fresh RemoteFile of the same identity) the item is
        observed again and queued for download, keeping its existing pins and
        pinning it directly if it had none. Without it, the item is forgotten.
        """
        key = self.key(identity)
        with self.transaction() as connection:
            connection.execute("INSERT INTO archives VALUES (?,?)", (archive_id, json.dumps(receipt)))
            for table in ("local_entries", "copies", "issues", "verifications", "jobs"):
                connection.execute(f"DELETE FROM {table} WHERE drive_id=? AND item_id=?", key)
            if retry is not None:
                self.observe(retry, connection=connection)
                connection.execute(
                    "UPDATE files SET dirty=0,operation='idle',error=NULL WHERE drive_id=? AND item_id=?", key
                )
                if not receipt["pin_sources"]:
                    connection.execute("INSERT INTO pin_sources VALUES (?,?,'direct')", key)
                connection.execute("UPDATE files SET pinned=1 WHERE drive_id=? AND item_id=?", key)
                connection.execute(
                    "INSERT INTO jobs(drive_id,item_id,requested_path) VALUES (?,?,?)",
                    (*key, retry.relative_path),
                )
            else:
                connection.execute("DELETE FROM pin_sources WHERE drive_id=? AND item_id=?", key)
                connection.execute("DELETE FROM files WHERE drive_id=? AND item_id=?", key)
            connection.execute("DELETE FROM publications WHERE drive_id=? AND item_id=?", key)

    def mark_idle(self, identity):
        self._update(identity, "operation='idle', error=NULL", ())

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
        self.connection = self.lock = self._directory_fd = None  # type: ignore[assignment]
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
            self.connection.create_function("cm_path_key", 1, config.path_key, deterministic=True)
            if self.connection.execute("PRAGMA user_version").fetchone()[0] not in (
                1,
                2,
                3,
                4,
                5,
                SCHEMA_VERSION,
            ):
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
