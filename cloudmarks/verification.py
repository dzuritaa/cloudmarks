"""Explicit full hashing and short-lived read-only evidence for large files.

Fingerprints include inode, device, size, mtime, ctime and link count. Every badge
still validates current account/state/disk/mount evidence; it never reads FUSE
or downloads content. This trusts the kernel and the user's private state, as do
small-file checks. It does not defend against a privileged local attacker.
"""

import json
import math
import time
from dataclasses import asdict

from .engine import StatusEngine
from .model import IntegrityError, ScopeError, VerificationRequired
from .onedrive import Cancelled
from .paths import SecureRoot
from .recovery import measure
from .view import attest_view, prove_view

INLINE_LIMIT = 8 * 1024 * 1024
PROOF_SECONDS = 300


def verify(control, path, *, clock=time.time):
    identity = control.identity_at(path)
    remote, copy = control.store.remote(identity), control.store.copy(identity)
    row = control.store.row(identity)
    age = clock() - remote.observed_at
    issue = control.store.connection.execute(
        "SELECT 1 FROM issues WHERE drive_id=? AND item_id=?", control.store.key(identity)
    ).fetchone()
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
    if store.connection.execute("PRAGMA user_version").fetchone()[0] < 4:
        raise VerificationRequired()
    row = store.connection.execute(
        "SELECT payload FROM verifications WHERE drive_id=? AND item_id=?", store.key(remote.identity)
    ).fetchone()
    if row is None:
        raise VerificationRequired()
    proof = json.loads(row[0])
    age = clock() - proof["verified_at"]
    if (
        proof.get("version") != 1
        or not math.isfinite(age)
        or not 0 <= age < PROOF_SECONDS
        or proof["copy"] != asdict(copy)
        or proof["relative_path"] != remote.relative_path
    ):
        raise VerificationRequired()
    if not attest_view(config, settings):
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


def status_engine(config, store, settings, *, clock=time.time):
    def check(root, remote, copy):
        if copy.size <= INLINE_LIMIT:
            return root.verify(copy.object_path, copy.size, copy.sha256)
        return cached_fingerprint(config, store, settings, root, remote, copy, clock=clock)

    return StatusEngine(
        config,
        store,
        clock=clock,
        verify_copy=check,
        read_path=lambda remote, copy: copy.size > INLINE_LIMIT or prove_view(config, settings, remote, copy),
    )
