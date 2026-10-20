"""Explicit M3 test-account configuration and physical storage binding."""

import json
import os
import subprocess
from pathlib import Path

from .config import absolute_path, integer, load_config
from .ipc import read_private_json
from .model import ConfigurationError, StorageError, text

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


def check_volume(config, settings):
    # A marker in an ordinary empty mountpoint alone is not a disk identity.
    probe = config.storage_root if config.storage_root.exists() else config.storage_root.parent
    try:
        result = subprocess.run(
            ["findmnt", "--json", "--target", str(probe), "--output", "UUID,TARGET,FSTYPE,OPTIONS,MAJ:MIN"],
            capture_output=True,
            timeout=5,
            check=True,
        )
        rows = json.loads(result.stdout)["filesystems"]
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
            or "rw" not in row["options"].split(",")
        ):
            raise StorageError("Expected writable storage volume is not mounted at its configured location")
        if config.storage_root.exists():
            check_binding(config, settings)
    except (OSError, ValueError, KeyError, IndexError, subprocess.SubprocessError):
        raise StorageError("Expected storage volume cannot be verified") from None


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


def initialize(config, settings):
    from .install import json_bytes, write_new
    from .paths import MARKER, open_absolute_directory

    check_volume(config, settings)
    require_unmounted(settings)
    fd = open_absolute_directory(config.storage_root.parent)
    os.close(fd)
    config.storage_root.mkdir(mode=0o700)  # Refuse to adopt/overwrite existing data.
    write_new(config.storage_root / ".cloudmarks-binding", json_bytes(store_binding(config, settings)))
    marker = config.storage_root / MARKER
    marker.write_text(config.volume_id + "\n")
    with marker.open("rb") as stream:
        os.fsync(stream.fileno())
    for name in ("objects", "local", "recovery"):
        (config.storage_root / name).mkdir(mode=0o700)
    for path in (config.storage_root, config.storage_root.parent):
        fd = open_absolute_directory(path)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
