"""Explicit, auditable cleanup of unreferenced bytes in the private offline store.

Nothing here runs automatically, from the status helper or from Nautilus. The
store keeps old versions, rollback backups and partial downloads so that no
interrupted operation can lose data (see the module docstring of offline.py).
Over time those files fill the storage budget. ``collect`` finds files that
no state record references and, only with ``apply=True``, removes those that
have been untouched for at least ``min_age_seconds``.

Every byte in the store came from the cloud: the view is read-only, so a
retained file is never the only copy of a local edit. Conflict snapshots,
issue candidates, archive receipts, publications in progress and explicit
verification proofs are references and are always kept. The visible
``local/`` tree is never touched.

Safety rules:
- Runs under the state writer lock, so no keep, free or recovery is running.
- Refuses while a publication intent is unrecovered; run ``offline recover``.
- Checks the disk identity first and revalidates the store root at the end.
- Removes only singly linked regular files directly inside ``objects/`` or
  ``recovery/``, whose stat fingerprint is unchanged since the inventory.
  Anything else is reported as unexpected and kept.
"""

import json
import os
import stat
import time
from dataclasses import dataclass
from typing import Any

from .model import StorageError
from .paths import DIRECTORY, SecureRoot, fingerprint

DEFAULT_MIN_AGE_SECONDS = 7 * 24 * 3600
AREAS = ("objects", "recovery")
CATEGORIES = ("partial", "superseded", "recovery")


@dataclass(frozen=True)
class Candidate:
    path: str
    category: str
    allocated: int
    age_seconds: float
    fingerprint: tuple


def store_paths(value, found):
    """Collect every ``objects/`` or ``recovery/`` path mentioned in a JSON value."""
    if isinstance(value, str):
        if value.startswith(AREAS[0] + "/") or value.startswith(AREAS[1] + "/"):
            found.add(value)
    elif isinstance(value, dict):
        for item in value.values():
            store_paths(item, found)
    elif isinstance(value, list):
        for item in value:
            store_paths(item, found)
    return found


def referenced(store):
    """Store-relative paths that any state record still needs."""
    found: set[str] = set()
    connection = store.connection
    version = store.schema_version()
    found.update(row[0] for row in connection.execute("SELECT object_path FROM copies"))
    payloads = ["SELECT payload FROM publications"]
    if version >= 3:
        found.update(row[0] for row in connection.execute("SELECT object_path FROM snapshots"))
        payloads.append("SELECT candidate FROM issues WHERE candidate IS NOT NULL")
    if version >= 4:
        payloads += ["SELECT payload FROM archives", "SELECT payload FROM verifications"]
    for query in payloads:
        for (payload,) in connection.execute(query):
            store_paths(json.loads(payload), found)
    return found


def category(area, name):
    if area == "objects":
        return "partial" if name.startswith(".partial-") else "superseded"
    return "recovery"


def inventory(root, references, *, now):
    """Unreferenced store files, and the count of entries that are kept as unexpected."""
    candidates, unexpected = [], 0
    for area in AREAS:
        try:
            fd = os.open(area, DIRECTORY, dir_fd=root.fd)
        except FileNotFoundError:
            continue
        try:
            if os.fstat(fd).st_dev != root.device:
                raise StorageError("Store area is on another filesystem; refusing cleanup")
            for name in sorted(os.listdir(fd)):
                path = area + "/" + name
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_dev != root.device:
                    unexpected += 1
                    continue
                if path in references:
                    continue
                candidates.append(
                    Candidate(
                        path,
                        category(area, name),
                        max(info.st_size, info.st_blocks * 512),
                        max(0.0, now - max(info.st_mtime, info.st_ctime)),
                        fingerprint(info),
                    )
                )
        finally:
            os.close(fd)
    return candidates, unexpected


def collect(controller, *, apply=False, min_age_seconds=DEFAULT_MIN_AGE_SECONDS, clock=time.time):
    """Report, and with ``apply`` remove, old unreferenced store files. Returns a JSON-safe summary."""
    if not isinstance(min_age_seconds, (int, float)) or min_age_seconds < 0:
        raise StorageError("Minimum age must be a non-negative number of seconds")
    config, store = controller.config, controller.store
    controller.volume_check(config, controller.settings)
    if store.connection.execute("SELECT 1 FROM publications LIMIT 1").fetchone():
        raise StorageError("An interrupted operation needs recovery first; run offline recover")
    summary: dict[str, Any] = {
        "dry_run": not apply,
        "min_age_seconds": min_age_seconds,
        "categories": {
            name: {"files": 0, "bytes": 0, "eligible_files": 0, "eligible_bytes": 0} for name in CATEGORIES
        },
        "removed_files": 0,
        "removed_bytes": 0,
        "changed_since_inventory": 0,
        "unexpected": 0,
    }
    with SecureRoot(config.storage_root, config.volume_id) as root:
        candidates, summary["unexpected"] = inventory(root, referenced(store), now=clock())
        eligible = []
        for item in candidates:
            counts = summary["categories"][item.category]
            counts["files"] += 1
            counts["bytes"] += item.allocated
            if item.age_seconds >= min_age_seconds:
                counts["eligible_files"] += 1
                counts["eligible_bytes"] += item.allocated
                eligible.append(item)
        if apply and eligible:
            for item in eligible:
                controller.check_cancelled()
                with root.parent(item.path) as (parent, name):
                    try:
                        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
                    except FileNotFoundError:
                        summary["changed_since_inventory"] += 1
                        continue
                    if fingerprint(current) != item.fingerprint:
                        summary["changed_since_inventory"] += 1
                        continue
                    os.unlink(name, dir_fd=parent)
                    os.fsync(parent)
                summary["removed_files"] += 1
                summary["removed_bytes"] += item.allocated
            # Real usage dropped; force the next budget check to audit again.
            controller.usage_bound = None
        root.revalidate(config.volume_id)
    return summary
