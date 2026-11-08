"""Explicit M3 test-account configuration and physical storage binding."""

import json
import os
import subprocess
import threading
import time
from pathlib import Path

from .config import absolute_path, integer, load_config
from .ipc import read_private_json
from .model import CloudMarksError, ConfigurationError, StorageError, text

KEYS = {
    "version",
    "core_config",
    "rclone_config",
    "rclone",
    "visible_root",
    "expected_email",
    "budget_bytes",
    "min_free_bytes",
    "storage_mount_uuid",
    "storage_mount_target",
}


def load_offline(path):
    value = read_private_json(path)
    if (
        not isinstance(value, dict)
        or set(value) != KEYS
        or type(value["version"]) is not int
        or value["version"] != 1
    ):
        raise ConfigurationError("Expected the version 1 offline control configuration")
    for key in ("core_config", "rclone_config", "rclone", "visible_root", "storage_mount_target"):
        absolute_path(value[key], key)
    for key in ("expected_email", "storage_mount_uuid"):
        text(value[key], key)
    integer(value["budget_bytes"], "budget_bytes", 1)
    integer(value["min_free_bytes"], "min_free_bytes", 0)
    core = load_config(value["core_config"])
    root, view = core.storage_root, Path(value["visible_root"])
    if any(char in str(root) for char in ('"', "'", "\\")):
        raise ConfigurationError("Store pathname is not supported by the quoted union configuration")
    target = Path(value["storage_mount_target"])
    if (
        not root.is_relative_to(target)
        or root == target
        or view.is_relative_to(root)
        or root.is_relative_to(view)
    ):
        raise ConfigurationError("Use a dedicated store on the selected volume and a separate view")
    if not core.state_path.parent.exists():
        raise ConfigurationError("Create the private state directory before initializing the store")
    for private_path in (core.state_path, Path(value["rclone_config"]), Path(value["core_config"])):
        if private_path.is_relative_to(root) or private_path.is_relative_to(view):
            raise ConfigurationError("Keep state, configuration and credentials outside the data store/view")
    return core, value


def store_binding(config, settings):
    return {
        "version": 1,
        "drive": config.expected_drive_id,
        "root": config.allowed_root_id,
        "volume": config.volume_id,
        "state": str(config.state_path),
        "view": settings["visible_root"],
        "case_sensitive": config.case_sensitive,
    }


def check_binding(config, settings):
    if read_private_json(config.storage_root / ".cloudmarks-binding") != store_binding(config, settings):
        raise StorageError("Store belongs to another state database, account, root or visible view")


# Status proofs are process-local and bounded. A single short-lived worker owns
# findmnt refreshes; no socket request waits for it or renews stale evidence.
_VOLUME_PROOFS = {}
_VOLUME_PENDING = {}
_VOLUME_RETRY = {}
_VOLUME_LOCK = threading.Lock()
_VOLUME_THREAD = None
# An invalidation also cancels publication of an already-running proof.
_VOLUME_GENERATION = 0


def _volume_key(probe, settings):
    info = os.stat(probe)
    return (
        str(probe),
        info.st_dev,
        info.st_ino,
        settings.get("storage_mount_uuid"),
        settings.get("storage_mount_target"),
    )


def _verify_volume(config, settings, probe):
    """Synchronous authority for both mutations and background refreshes."""
    started = time.monotonic()
    before = _volume_key(probe, settings)
    try:
        result = subprocess.run(
            ["findmnt", "--json", "--target", str(probe), "--output", "UUID,TARGET,FSTYPE,OPTIONS,MAJ:MIN"],
            capture_output=True,
            timeout=5,
            check=True,
        )
        rows = json.loads(result.stdout)["filesystems"]
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise StorageError("Storage mount response is invalid")
        device = os.stat(probe).st_dev
        expected_device = f"{os.major(device)}:{os.minor(device)}"
        # systemd automounts can return both the autofs trigger and the real
        # filesystem at the same pathname. Bind identity to the accessed device.
        rows = [row for row in rows if row.get("maj:min") == expected_device]
        if len(rows) != 1:
            raise StorageError("Storage device mapping is ambiguous or unavailable")
        row = rows[0]
        if (
            len(rows) != 1
            or row["uuid"] != settings["storage_mount_uuid"]
            or row["target"] != settings["storage_mount_target"]
            or not isinstance(row.get("options"), str)
            or "rw" not in row["options"].split(",")
        ):
            raise StorageError("Expected writable storage volume is not mounted at its configured location")
        if config.storage_root.exists():
            check_binding(config, settings)
    except (OSError, ValueError, TypeError, KeyError, IndexError, subprocess.SubprocessError):
        raise StorageError("Expected storage volume cannot be verified") from None
    if _volume_key(probe, settings) != before:
        raise StorageError("Storage device changed during verification")
    return started


def _refresh_volumes():
    global _VOLUME_THREAD
    while True:
        with _VOLUME_LOCK:
            if not _VOLUME_PENDING:
                _VOLUME_THREAD = None
                return
            key = next(iter(_VOLUME_PENDING))
            config, settings, probe, generation = _VOLUME_PENDING[key]
        stamp = None
        try:
            stamp = _verify_volume(config, settings, probe)
            if _volume_key(probe, settings) != key:
                stamp = None
        except (CloudMarksError, OSError, ValueError, KeyError, IndexError):
            stamp = None  # A failed refresh revokes the previous proof immediately.
        with _VOLUME_LOCK:
            _VOLUME_PENDING.pop(key, None)
            _VOLUME_PROOFS.pop(key, None)
            if generation != _VOLUME_GENERATION:
                stamp = None
            if stamp is not None:
                if len(_VOLUME_PROOFS) >= 16:
                    _VOLUME_PROOFS.clear()
                _VOLUME_PROOFS[key] = stamp
            else:
                if len(_VOLUME_RETRY) >= 16:
                    _VOLUME_RETRY.clear()
                _VOLUME_RETRY[key] = time.monotonic() + 1


def check_volume(config, settings, *, reuse_seconds=0):
    """Mutations verify synchronously; status refreshes never block on findmnt.

    Status checks refresh after half the reuse interval. An absent/expired proof
    fails closed until the worker succeeds: there is no stale grace. Every reuse
    still checks the probe device/inode and private binding. Failure or mismatch
    revokes the proof, including failures observed by synchronous callers.
    """
    global _VOLUME_THREAD, _VOLUME_GENERATION
    probe = config.storage_root if config.storage_root.exists() else config.storage_root.parent
    try:
        key = _volume_key(probe, settings)
        if config.storage_root.exists():
            check_binding(config, settings)
    except (OSError, ValueError, KeyError, IndexError, CloudMarksError):
        with _VOLUME_LOCK:
            _VOLUME_PROOFS.clear()
            _VOLUME_GENERATION += 1
        raise StorageError(
            "Expected storage volume cannot be verified; connect the configured disk"
        ) from None
    if reuse_seconds <= 0:
        try:
            _verify_volume(config, settings, probe)
        finally:
            # Mutations cannot authorize a status cache entry, nor leave a
            # previous proof alive after observing a failed synchronous check.
            with _VOLUME_LOCK:
                _VOLUME_PROOFS.pop(key, None)
                _VOLUME_GENERATION += 1
        return
    now = time.monotonic()
    with _VOLUME_LOCK:
        stamp = _VOLUME_PROOFS.get(key)
        age = None if stamp is None else now - stamp
        if (age is None or not 0 <= age < reuse_seconds / 2) and key not in _VOLUME_PENDING:
            if len(_VOLUME_PENDING) < 16 and now >= _VOLUME_RETRY.get(key, 0):
                _VOLUME_PENDING[key] = (config, dict(settings), probe, _VOLUME_GENERATION)
                if _VOLUME_THREAD is None:
                    _VOLUME_THREAD = threading.Thread(
                        target=_refresh_volumes, name="cloudmarks-volume-proof", daemon=True
                    )
                    _VOLUME_THREAD.start()
        if age is not None and 0 <= age < reuse_seconds:
            return
    raise StorageError("Storage mount proof is being checked; retry status shortly")


def mounted_view(settings):
    # Decode Linux mountinfo octal escapes; path.is_mount() alone can miss an
    # unexpected/replaced mount and must not authorize publication or eviction.
    import re

    expected = str(Path(settings["visible_root"]))
    with open("/proc/self/mountinfo", encoding="utf-8") as stream:
        for line in stream:
            left, right = line.rstrip().split(" - ", 1)
            parts = left.split()
            mount = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), parts[4])
            if mount == expected:
                rest = right.split()
                return {
                    "id": parts[0],
                    "type": rest[0],
                    "source": rest[1],
                    "options": set(parts[5].split(",")),
                }
    return None


def require_unmounted(settings):
    if mounted_view(settings):
        raise StorageError("Unmount the experimental CloudMarks view before changing local content")


STORE_AREAS = ("objects", "local", "recovery")


def initialize(config, settings, *, resume=False):
    """Create and bind a new, empty offline store. Never adopts existing data.

    ``resume=True`` lets setup finish a store that its own interrupted run
    began: the folder must be private, owned by this user and contain only
    what this function creates, with a matching binding and empty areas.
    Missing pieces are then created; anything else is refused.
    """
    from .install import json_bytes, write_new
    from .paths import MARKER, open_absolute_directory

    check_volume(config, settings)
    require_unmounted(settings)
    fd = open_absolute_directory(config.storage_root.parent)
    os.close(fd)
    root = config.storage_root
    binding = json_bytes(store_binding(config, settings))
    if resume and (root.exists() or root.is_symlink()):
        present = _resumable_store(root, binding, config.volume_id)
    else:
        root.mkdir(mode=0o700)  # Refuse to adopt/overwrite existing data.
        present = set()
    if ".cloudmarks-binding" not in present:
        write_new(root / ".cloudmarks-binding", binding)
    marker = root / MARKER
    if MARKER not in present:
        write_new(marker, (config.volume_id + "\n").encode())
    with marker.open("rb") as stream:
        os.fsync(stream.fileno())
    for name in STORE_AREAS:
        if name not in present:
            (root / name).mkdir(mode=0o700)
    for path in (root, root.parent):
        fd = open_absolute_directory(path)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _resumable_store(root, binding, volume_id):
    """Names an interrupted initialize left in ``root``; raises unless it is safely ours."""
    import stat

    from .paths import DIRECTORY, MARKER, open_absolute_directory

    refuse = StorageError("The store folder already holds other data; choose a new folder")
    try:
        fd = open_absolute_directory(root)
    except OSError:
        raise refuse from None
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise refuse
        present = set(os.listdir(fd))
        if present - {".cloudmarks-binding", MARKER, *STORE_AREAS}:
            raise refuse
        if present and ".cloudmarks-binding" not in present:
            raise refuse  # The binding is always written first.
        for name in present:
            entry = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if name in STORE_AREAS:
                if not stat.S_ISDIR(entry.st_mode):
                    raise refuse
                area = os.open(name, DIRECTORY, dir_fd=fd)
                try:
                    if os.listdir(area):
                        raise refuse
                finally:
                    os.close(area)
                continue
            if not stat.S_ISREG(entry.st_mode) or entry.st_nlink != 1 or entry.st_uid != os.getuid():
                raise refuse
            expected = binding if name == ".cloudmarks-binding" else (volume_id + "\n").encode()
            value = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
            try:
                if os.read(value, len(expected) + 1) != expected:
                    raise refuse
            finally:
                os.close(value)
        return present
    finally:
        os.close(fd)
