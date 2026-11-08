"""Explicit full hashing and short-lived read-only evidence for large files.

Fingerprints include inode, device, size, mtime, ctime and link count. Every badge
still validates current account/state/disk/mount evidence; it never reads FUSE
or downloads content. This trusts the kernel and the user's private state, as do
small-file checks. It does not defend against a privileged local attacker.
"""

import hashlib
import json
import math
import os
import threading
import time
from collections.abc import Callable
from dataclasses import asdict

from .engine import StatusEngine
from .model import Cancelled, IntegrityError, ScopeError, VerificationPending, VerificationRequired
from .paths import SecureRoot, fingerprint
from .recovery import measure
from .view import attest_view, prove_view

Fingerprint = tuple[int, ...]
MemoKey = tuple[int, str, int, str, Fingerprint]

INLINE_LIMIT = 8 * 1024 * 1024
PROOF_SECONDS = 300
# Badge requests renew roughly every second. Reuse a complete SHA-256 result
# while the kernel fingerprint is unchanged, and hash again at this interval so
# silent media corruption is still found without hashing every visible file
# twice per second.
REHASH_SECONDS = 60
VOLUME_REUSE_SECONDS = 10
MEMO_ENTRIES = 4096
# Files up to this size are hashed on the request thread (about a millisecond
# from page cache). Larger misses go to the background worker.
INLINE_HASH_BYTES = 1024 * 1024
QUEUE_LIMIT = 256


class HashBudget:
    """Reserve bytes before each hash and check elapsed time between reads.

    A blocked kernel call cannot be interrupted; no hash reads past its recorded
    size, even if another process keeps appending to the file.
    """

    def __init__(self, max_bytes: int, seconds: float, *, monotonic: Callable[[], float] = time.monotonic):
        self.remaining = max_bytes
        self.monotonic = monotonic
        self.deadline = monotonic() + seconds

    def reserve(self, size: int = 0) -> None:
        if size > self.remaining or self.monotonic() >= self.deadline:
            raise VerificationPending()
        self.remaining -= size


def hash_file(
    root: SecureRoot, path: str, size: int, sha256: str, budget: HashBudget | None = None
) -> Fingerprint:
    if budget is None:
        return tuple(root.verify(path, size, sha256))
    budget.reserve(size)
    with root.open_regular(path) as fd:
        before = os.fstat(fd)
        if before.st_size != size:
            raise IntegrityError("Local file length does not match the completed copy")
        value = hashlib.sha256()
        count = 0
        while count < size:
            budget.reserve()
            block = os.read(fd, min(1024 * 1024, size - count))
            if not block:
                break
            count += len(block)
            value.update(block)
        budget.reserve()
        if fingerprint(before) != fingerprint(os.fstat(fd)):
            raise IntegrityError("Local file changed during verification")
        if count != size or value.hexdigest() != sha256:
            raise IntegrityError("Local file is partial, changed, or corrupt")
        return tuple(fingerprint(before))


class HashMemo:
    """Status-only reuse of full-content checks, keyed by kernel fingerprint.

    The trust model matches explicit large-file proofs: any write through the
    filesystem changes size, mtime or ctime, which invalidates the entry.
    Mutating operations never use this memo; they always hash.
    """

    def __init__(
        self,
        *,
        seconds: float = REHASH_SECONDS,
        limit: int = MEMO_ENTRIES,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.seconds, self.limit, self.monotonic = seconds, limit, monotonic
        self.entries: dict[MemoKey, float] = {}
        self.failures: dict[MemoKey, float] = {}
        self.lock = threading.Lock()

    def verify(
        self,
        root: SecureRoot,
        relative_path: str,
        size: int,
        sha256: str,
        *,
        budget: HashBudget | None = None,
    ) -> Fingerprint:
        current = tuple(root.current_fingerprint(relative_path))
        key = (root.device, relative_path, size, sha256, current)
        with self.lock:
            stamp, failed = self.entries.get(key), self.failures.get(key)
        if stamp is not None and 0 <= self.monotonic() - stamp < self.seconds:
            return current
        if failed is not None and 0 <= self.monotonic() - failed < self.seconds:
            raise IntegrityError("Local file is partial, changed, or corrupt")
        try:
            verified = hash_file(root, relative_path, size, sha256, budget)
        except IntegrityError:
            with self.lock:
                self.entries.pop(key, None)
                while len(self.failures) >= self.limit:
                    del self.failures[next(iter(self.failures))]
                self.failures[key] = self.monotonic()
            raise
        with self.lock:
            self.entries.pop(key, None)
            while len(self.entries) >= self.limit:
                del self.entries[next(iter(self.entries))]
            self.entries[(root.device, relative_path, size, sha256, verified)] = self.monotonic()
        return verified


class BackgroundHashMemo(HashMemo):
    """HashMemo whose expensive misses never run on the status socket thread.

    A miss above ``inline_bytes`` queues one deduplicated job and raises
    VerificationPending, which the engine reports as a short-lived "checking"
    state. A worker thread hashes with its own SecureRoot and stores the
    result under the verified fingerprint, so the next request is a memo hit.

    After the rehash interval an unchanged file keeps its result for one more
    interval while the worker rehashes it, instead of flickering to
    "checking". Failures are remembered by fingerprint for one interval so a
    corrupt copy reports an error rather than "checking" forever.
    """

    def __init__(self, config, *, inline_bytes=INLINE_HASH_BYTES, queue_limit=QUEUE_LIMIT, **kwargs):
        super().__init__(**kwargs)
        self.config, self.inline_bytes, self.queue_limit = config, inline_bytes, queue_limit
        self.pending: dict[MemoKey, tuple[str, int, str]] = {}
        self.ready = threading.Condition(self.lock)
        self.stopped = False
        self.thread: threading.Thread | None = None

    def verify(
        self,
        root: SecureRoot,
        relative_path: str,
        size: int,
        sha256: str,
        *,
        budget: HashBudget | None = None,
    ) -> Fingerprint:
        current = tuple(root.current_fingerprint(relative_path))
        key = (root.device, relative_path, size, sha256, current)
        now = self.monotonic()
        with self.lock:
            stamp, failed = self.entries.get(key), self.failures.get(key)
        if stamp is not None and 0 <= now - stamp < self.seconds:
            return current
        if failed is not None and 0 <= now - failed < self.seconds:
            raise IntegrityError("Local file is partial, changed, or corrupt")
        if size <= self.inline_bytes or budget is not None:
            return super().verify(root, relative_path, size, sha256, budget=budget)
        self.submit(key, relative_path, size, sha256)
        if stamp is not None and 0 <= now - stamp < 2 * self.seconds:
            return current
        raise VerificationPending()

    def submit(self, key: MemoKey, relative_path: str, size: int, sha256: str) -> None:
        with self.ready:
            if self.stopped or key in self.pending or len(self.pending) >= self.queue_limit:
                return
            self.pending[key] = (relative_path, size, sha256)
            if self.thread is None:
                self.thread = threading.Thread(target=self.worker, name="cloudmarks-hash", daemon=True)
                self.thread.start()
            self.ready.notify()

    def worker(self):
        while True:
            with self.ready:
                while not self.pending and not self.stopped:
                    self.ready.wait()
                if self.stopped:
                    return
                key, job = next(iter(self.pending.items()))
            try:
                with SecureRoot(self.config.storage_root, self.config.volume_id) as root:
                    HashMemo.verify(self, root, *job)
            except IntegrityError:
                pass  # HashMemo remembers failed fingerprints for both callers.
            except Exception:
                # Storage unavailable or the file vanished: the next request
                # re-queues it and the engine re-checks all other evidence.
                pass
            finally:
                with self.lock:
                    self.pending.pop(key, None)

    def drain(self, timeout=5.0):
        """Wait until queued checks finish. For tests and benchmarks."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                if not self.pending:
                    return True
            time.sleep(0.005)
        return False

    def close(self):
        with self.ready:
            self.stopped = True
            self.pending.clear()
            self.ready.notify_all()
        if self.thread is not None:
            self.thread.join(timeout=1)


def verify(control, path, *, clock=time.time):
    identity = control.identity_at(path)
    remote, copy = control.store.remote(identity), control.store.copy(identity)
    row = control.store.row(identity)
    age = clock() - remote.observed_at
    issue = control.store.issue(identity)
    if (
        not copy
        or remote.deleted
        or remote.is_directory
        or row["dirty"]
        or row["operation"] != "idle"
        or issue
        or not math.isfinite(age)
        or not 0 <= age <= control.config.max_age_seconds
        or (copy.revision, copy.size) != (remote.revision, remote.size)
        or remote.sha256 is not None
        and copy.sha256 != remote.sha256
    ):
        raise ScopeError("Verification requires a fresh, current, clean and complete retained file")
    config = control.config
    if not attest_view(config, control.settings):
        raise ScopeError("Mount the controlled read-only view before explicit verification")
    with SecureRoot(config.storage_root, config.volume_id) as root:
        sources = {}
        for label, source in (("object", copy.object_path), ("local", "local/" + remote.relative_path)):
            control.progress(operation="verify", phase=label, total_bytes=copy.size)
            measured = measure(
                root,
                source,
                control.cancelled,
                progress=lambda phase=label, **values: control.progress(
                    operation="verify", phase=phase, **values
                ),
            )
            if measured.get("missing") or (measured["size"], measured["sha256"]) != (copy.size, copy.sha256):
                raise IntegrityError("Verification failed; preserve the changed local version")
            sources[label] = measured
        root.revalidate(config.volume_id)
        if not attest_view(config, control.settings):
            raise ScopeError("View changed during verification")
        for source in sources.values():
            if list(root.current_fingerprint(source["path"])) != source["fingerprint"]:
                raise IntegrityError("Content changed during verification")
        current = control.store.row(identity)
        if (
            control.store.remote(identity) != remote
            or control.store.copy(identity) != copy
            or current["dirty"]
            or current["operation"] != "idle"
            or not 0 <= clock() - remote.observed_at <= config.max_age_seconds
        ):
            raise ScopeError("State expired or changed during verification")
        if control.cancelled():
            raise Cancelled("Verification cancelled; no proof written")
        proof = {
            "version": 1,
            "verified_at": clock(),
            "copy": asdict(copy),
            "relative_path": remote.relative_path,
            "sources": sources,
        }
        with control.store.transaction() as connection:
            connection.execute(
                "INSERT INTO verifications VALUES (?,?,?) ON CONFLICT(drive_id,item_id) "
                "DO UPDATE SET payload=excluded.payload",
                (*control.store.key(identity), json.dumps(proof)),
            )
    return {
        "verified": True,
        "bytes": copy.size,
        "valid_for_seconds": max(
            0, min(PROOF_SECONDS, config.max_age_seconds - (clock() - remote.observed_at))
        ),
    }


def cached_fingerprint(config, store, settings, root, remote, copy, *, clock=time.time):
    if store.schema_version() < 4:
        raise VerificationRequired()
    payload = store.verification_payload(remote.identity)
    if payload is None:
        raise VerificationRequired()
    proof = json.loads(payload)
    age = clock() - proof["verified_at"]
    if (
        proof.get("version") != 1
        or not math.isfinite(age)
        or not 0 <= age < PROOF_SECONDS
        or proof["copy"] != asdict(copy)
        or proof["relative_path"] != remote.relative_path
    ):
        raise VerificationRequired()
    if not attest_view(config, settings, volume_reuse_seconds=VOLUME_REUSE_SECONDS):
        raise VerificationRequired()
    sources = proof["sources"]
    if (
        sources["object"]["path"] != copy.object_path
        or sources["local"]["path"] != "local/" + remote.relative_path
    ):
        raise VerificationRequired()
    if set(sources) != {"object", "local"}:
        raise VerificationRequired()
    for source in sources.values():
        if (source.get("size"), source.get("sha256")) != (copy.size, copy.sha256):
            raise VerificationRequired()
        if list(root.current_fingerprint(source["path"])) != source["fingerprint"]:
            raise IntegrityError("Content changed after explicit verification")
    root.revalidate(config.volume_id)
    return tuple(sources["object"]["fingerprint"])


def copy_fingerprint(
    config,
    store,
    settings,
    root,
    remote,
    copy,
    memo: HashMemo,
    *,
    clock=time.time,
    budget: HashBudget | None = None,
):
    """One badge policy for single files and folder members, never for mutations."""
    if copy.size <= INLINE_LIMIT:
        return memo.verify(root, copy.object_path, copy.size, copy.sha256, budget=budget)
    return cached_fingerprint(config, store, settings, root, remote, copy, clock=clock)


class VerifiedStatusEngine(StatusEngine):
    memo: HashMemo


def status_engine(config, store, settings, *, clock=time.time, memo=None, background=False, report=None):
    """Status engine for the offline view.

    ``background=True`` is for the status socket: large content checks run on
    a worker thread and report "checking" until done. CLI callers keep the
    default, which hashes inline and returns a final answer. ``report(category)``
    is told about evidence that was unavailable (see StatusEngine).
    """
    memo = memo or (BackgroundHashMemo(config) if background else HashMemo())

    def check(root, remote, copy):
        return copy_fingerprint(config, store, settings, root, remote, copy, memo, clock=clock)

    def read_path(remote, copy):
        return copy.size > INLINE_LIMIT or prove_view(
            config, settings, remote, copy, verify=memo.verify, volume_reuse_seconds=VOLUME_REUSE_SECONDS
        )

    engine = VerifiedStatusEngine(
        config, store, clock=clock, verify_copy=check, read_path=read_path, report=report
    )
    engine.memo = memo
    return engine
