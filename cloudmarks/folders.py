"""Bounded folder proofs in a background worker, never in status/UI requests.

Cloud membership has its own deadline. Local hashes are reused only while both
kernel fingerprints and the recorded copy match. No FUSE content is opened.
"""

import json
import os
import threading
import time

from .activity import FRESH_SECONDS
from .backend import create as create_backend
from .limits import TREE_ENTRIES
from .model import IntegrityError, ScopeError
from .paths import DIRECTORY, SecureRoot
from .state import ReadStore
from .tracking import manifest
from .verification import VOLUME_REUSE_SECONDS
from .view import attest_view


class FolderObserver:
    def __init__(self, config, offline, activity, *, backend=None, clock=time.time):
        self.config, self.offline, self.activity, self.clock = config, offline, activity, clock
        self.backend = backend or create_backend(config, offline)
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.interests, self.results, self.trees, self.hashes = {}, {}, {}, {}
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self.worker, name="cloudmarks-folder-proof", daemon=True)
        self.thread.start()
        return self

    def close(self):
        self.stop.set()
        self.wake.set()
        if self.thread:
            self.thread.join(timeout=0.5)

    def request(self, path, view):
        now = self.clock()
        key = (path, view)
        with self.lock:
            self.interests[key] = now
            if len(self.interests) > 16:
                oldest = min(self.interests, key=self.interests.get)
                del self.interests[oldest]
                self.results.pop(oldest, None)
            cached = self.results.get(key)
            self.wake.set()
        if cached and 0 <= now - cached[0] < 2 and now < cached[1]:
            result = dict(cached[2])
            result["valid_for_ms"] = max(
                1, min(1000, int(min(2 - (now - cached[0]), cached[1] - now) * 1000))
            )
            return result
        return self.activity.result("unknown", "Checking current folder membership and offline copies")

    def worker(self):
        while not self.stop.is_set():
            self.wake.clear()
            now = self.clock()
            with self.lock:
                wanted = [(key, stamp) for key, stamp in self.interests.items() if 0 <= now - stamp < 10]
            for key, _ in sorted(wanted, key=lambda item: item[1], reverse=True):
                if self.stop.is_set():
                    return
                with self.lock:
                    previous = self.results.get(key)
                if previous and 0 <= now - previous[0] < 1:
                    continue
                try:
                    result, deadline = self.check(*key)
                except IntegrityError:
                    result = self.activity.result(
                        "error", "A local folder copy changed or failed verification; preserve it"
                    )
                    deadline = self.clock() + 1
                except Exception:
                    result = self.activity.result(
                        "unknown", "Folder evidence unavailable; retained files are preserved"
                    )
                    deadline = self.clock() + 1
                with self.lock:
                    self.results[key] = (self.clock(), deadline, result)
            self.wake.wait(0.5)

    def check(self, path, view):
        now = self.clock()
        tree = self.trees.get(path)
        if tree is None or not 0 <= now - tree[0] < 15:
            observations = self.backend.tree(path, limit=TREE_ENTRIES, cancelled=self.stop.is_set)
            # Timestamp the beginning, not completion, of remote enumeration.
            tree = (now, observations, manifest(observations))
            self.trees[path] = tree
            if len(self.trees) > 16:
                del self.trees[min(self.trees, key=lambda p: self.trees[p][0])]
        deadline = tree[0] + min(FRESH_SECONDS, self.config.max_age_seconds)
        observations = tree[1]
        if self.clock() >= deadline:
            raise ScopeError("Folder metadata expired during enumeration")
        current_manifest = tree[2]
        roots = [v.remote for v in observations if v.remote.relative_path == path and v.remote.is_directory]
        if len(roots) != 1:
            raise ScopeError("Folder scan has no exact root")
        # Upload views cannot claim an offline read path through their cache.
        if view == "upload":
            return self.activity.result(
                "cloud_only",
                f"Confirmed in OneDrive: {sum(not v.remote.is_directory for v in observations)} files",
            ), deadline
        with (
            ReadStore(self.config) as store,
            SecureRoot(self.config.storage_root, self.config.volume_id) as disk,
        ):
            version = store.data_version()
            intent = store.folder_intent(roots[0].identity)
            selected = bool(intent and intent["pinned"])
            same_manifest = bool(intent and json.loads(intent["manifest"]) == current_manifest)
            if not attest_view(self.config, self.offline, volume_reuse_seconds=VOLUME_REUSE_SECONDS):
                raise ScopeError("Offline read path is unavailable")
            verified, total, retained, directories = 0, 0, 0, True
            for value in observations:
                if self.stop.is_set():
                    raise ScopeError("Folder verification stopped")
                remote = value.remote
                self.config.validate_remote(remote)
                if remote.relative_path != path and not remote.relative_path.startswith(path + "/"):
                    raise ScopeError("Folder scan escaped its selection")
                if remote.is_directory:
                    try:
                        with disk.parent("local/" + remote.relative_path) as (fd, name):
                            child = os.open(name, DIRECTORY, dir_fd=fd)
                            try:
                                if os.fstat(child).st_dev != disk.device:
                                    raise ScopeError("Folder is on another volume")
                            finally:
                                os.close(child)
                    except FileNotFoundError:
                        directories = False
                    continue
                total += 1
                copy = store.copy(remote.identity)
                if copy is None:
                    continue
                retained += 1
                row = store.row(remote.identity)
                issue = store.issue(remote.identity)
                if issue or row["dirty"] or row["operation"] == "error":
                    return self.activity.result(
                        "error", "A folder member needs review; local copies are preserved"
                    ), deadline
                if row["operation"] != "idle":
                    return self.activity.result(
                        "transferring", f"Saving offline folder: {verified}/{total} checked files"
                    ), deadline
                entry = store.local_entry(remote.identity)
                if (
                    not entry
                    or entry[0] != remote.relative_path
                    or (copy.revision, copy.size) != (remote.revision, remote.size)
                    or remote.sha256 is not None
                    and remote.sha256 != copy.sha256
                ):
                    continue
                paths = (copy.object_path, "local/" + remote.relative_path)
                signatures = tuple(disk.current_fingerprint(p) for p in paths)
                key = (copy, paths, signatures)
                if key not in self.hashes:
                    for source in paths:
                        disk.verify(source, copy.size, copy.sha256)
                    if tuple(disk.current_fingerprint(p) for p in paths) != signatures:
                        raise IntegrityError("Folder content changed during verification")
                    self.hashes[key] = True
                    if len(self.hashes) > TREE_ENTRIES:
                        del self.hashes[next(iter(self.hashes))]
                verified += 1
            # Extra retained publications, including renamed/deleted members,
            # invalidate aggregate completion rather than disappearing in a list.
            entries = store.local_entries_under(path)
            expected = {
                v.remote.identity.item_id: v.remote.relative_path
                for v in observations
                if not v.remote.is_directory
            }
            extras = any(expected.get(entry[0]) != entry[1] for entry in entries)
            disk.revalidate(self.config.volume_id)
            if (
                store.data_version() != version
                or not attest_view(self.config, self.offline, volume_reuse_seconds=VOLUME_REUSE_SECONDS)
                or self.clock() >= deadline
            ):
                raise ScopeError("Folder evidence changed or expired")
            if selected and same_manifest and directories and verified == total and not extras:
                return self.activity.result(
                    "kept_offline", f"All {total} files and folders verified offline"
                ), deadline
            if retained or selected or extras:
                return self.activity.result(
                    "partial",
                    f"Verified offline: {verified}/{total} files; apply Keep offline to refresh the complete folder",
                ), deadline
            return self.activity.result(
                "cloud_only", f"Confirmed in OneDrive: {total} files; no complete offline selection"
            ), deadline
