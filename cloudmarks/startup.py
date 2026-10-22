"""Guard service mountpoints without hiding or modifying pending local files."""

import os
import stat
from pathlib import Path

from .model import ConfigurationError, StorageError
from .offline_config import load_offline, mounted_view
from .paths import open_absolute_directory


def guard_mountpoint(path, *, prepare):
    """Prepare an empty directory, or protect an unmounted one after stopping.

    FUSE requires a writable mountpoint at startup. Stop/failure protection makes
    the underlying directory read-only again. A file arriving during startup
    causes rclone's normal nonempty-mount check to refuse the mount; never use
    --allow-non-empty with this guard.
    """
    path = Path(path)
    fd = open_absolute_directory(path)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise StorageError("Mountpoint must be owned by this user and private")
        if mounted_view({"visible_root": str(path)}):
            if prepare:
                raise StorageError("Mountpoint is already in use; preserve the existing mount")
            # A busy normal unmount must not change permissions on the cloud.
            return {"mounted": True, "protected": False}
        if prepare and os.listdir(fd):
            raise StorageError("Mountpoint contains local files; preserve them before mounting")
        os.fchmod(fd, 0o700 if prepare else 0o500)
        return {"mounted": False, "protected": not prepare}
    finally:
        os.close(fd)


def run(settings, view, operation):
    if settings["mode"] != "onedrive":
        raise ConfigurationError("Mount guards require an explicit OneDrive profile")
    _, offline = load_offline(settings["core_config"])
    if settings["visible_root"] != offline["visible_root"]:
        raise ConfigurationError("Desktop and offline view roots disagree")
    if view == "upload" and settings.get("version") == 3:
        root = settings["upload_root"]
    elif view == "offline":
        root = settings["visible_root"]
    else:
        raise ConfigurationError("Upload guard requires automatic desktop settings")
    if operation not in ("prepare", "protect"):
        raise ConfigurationError("Unknown mount guard operation")
    return guard_mountpoint(root, prepare=operation == "prepare")
