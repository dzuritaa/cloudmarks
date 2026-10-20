"""Controlled read-only view. No reads of cloud paths for status proof."""

import configparser
import os
import signal
import subprocess
import tempfile
import time
from dataclasses import replace
from pathlib import Path

from .install import atomic_json
from .ipc import read_private_json
from .model import FileIdentity, IntegrityError, ScopeError, StorageError
from .offline import OfflineController
from .offline_config import check_volume, mounted_view, require_unmounted
from .onedrive import OneDrive, checksum, clean_environment
from .paths import SecureRoot, open_absolute_directory
from .state import StateStore


def device_name(config):
    import hashlib

    return "cloudmarks-" + hashlib.sha256(config.volume_id.encode()).hexdigest()[:24]


def attest_view(config, settings):
    check_volume(config, settings)
    record = read_private_json(config.state_path.parent / "view.json")
    current = mounted_view(settings)
    if (
        not current
        or current["source"] != device_name(config)
        or "ro" not in current["options"]
        or current["id"] != record.get("mount_id")
        or current["type"] != "fuse.rclone"
        or record.get("storage") != str(config.storage_root)
        or record.get("volume") != config.volume_id
    ):
        return False
    return True


def prove_view(config, settings, remote, copy):
    if not attest_view(config, settings):
        return False
    # Verify the durable local winner. Do not open a union/cloud path from a
    # badge request: even a read could hydrate data if the local winner vanished.
    with SecureRoot(config.storage_root, config.volume_id) as root:
        root.verify("local/" + remote.relative_path, copy.size, copy.sha256)
        root.revalidate(config.volume_id)
    return True


def refresh_online_probe(config, store, transport):
    probe = store.connection.execute(
        "SELECT item_id,relative_path FROM files WHERE is_directory=0 AND deleted=0 LIMIT 1"
    ).fetchone()
    if probe is None:
        raise ScopeError("Keep one known test file first to verify the effective online root")
    identity = FileIdentity(config.expected_drive_id, probe[0])
    observation = transport.describe(probe[1])
    config.validate_remote(observation.remote)
    if observation.remote.identity != identity:
        store.fail_transfer(
            identity, "Remote path now identifies another item; preserve the retained snapshot"
        )
        raise ScopeError("Remote path replacement requires explicit recovery")
    transport.verify_transport_item(observation)
    copy = store.copy(identity)
    remote = observation.remote
    if copy and (copy.revision, copy.size) == (remote.revision, remote.size):
        value = checksum(observation.kind)
        with SecureRoot(config.storage_root, config.volume_id) as root:
            with root.open_regular(copy.object_path) as fd:
                while block := os.read(fd, 256 * 1024):
                    value.update(block)
            root.revalidate(config.volume_id)
        if value.digest() != observation.digest:
            store.fail_transfer(identity, "Remote checksum differs from the retained snapshot; preserve it")
            raise IntegrityError("Retained content does not match fresh remote checksum evidence")
        remote = replace(remote, sha256=copy.sha256)
    # Do not ignore fresh metadata observed while verifying the online mount:
    # a newer revision must invalidate the old copy's current-version badge.
    store.observe(remote)


def mount(config, settings, *, offline=False, ready=None, stop=lambda: False):
    check_volume(config, settings)
    require_unmounted(settings)
    transport = OneDrive(config, settings)
    if not offline:
        transport.authenticate()
    view = Path(settings["visible_root"])
    if not view.exists():
        fd = open_absolute_directory(view.parent)
        os.close(fd)
        view.mkdir(mode=0o700)
    fd = open_absolute_directory(view)
    try:
        if os.listdir(fd) or os.fstat(fd).st_uid != os.getuid() or os.fstat(fd).st_mode & 0o077:
            raise ScopeError("Experimental mountpoint must be empty, private and user-owned")
    finally:
        os.close(fd)
    runtime_conf = None
    process = None
    # Hold the writer lock until FUSE is mounted. Control commands subsequently
    # acquire it, see the mount, and refuse any publication/eviction.
    try:
        with StateStore(config) as store:
            controller = OfflineController(config, store, transport, settings)
            controller.recover()
            if not offline:
                from .tracking import ChangeTracker

                ChangeTracker(controller).check_online()
            with SecureRoot(config.storage_root, config.volume_id) as root:
                rows = store.connection.execute("SELECT drive_id,item_id FROM local_entries").fetchall()
                for row in rows:
                    controller.verify_entry(root, controller.entry(FileIdentity(*row)))
                root.revalidate(config.volume_id)
            conf = settings["rclone_config"]
            if offline:
                # Explicit no-network offline mode, using the same visible path.
                # The derived config contains neither a remote nor credentials.
                parser = configparser.ConfigParser(interpolation=None)
                parser["view"] = {"type": "alias", "remote": str(config.storage_root / "local")}
                fd, temporary = tempfile.mkstemp(
                    prefix="offline-view-", suffix=".conf", dir=config.state_path.parent
                )
                runtime_conf = Path(temporary)
                with os.fdopen(fd, "w") as stream:
                    parser.write(stream)
                conf = str(runtime_conf)
            argv = [
                settings["rclone"],
                "mount",
                "view:",
                str(view),
                "--config",
                conf,
                "--cache-dir",
                str(config.state_path.parent / "rclone-cache"),
                "--read-only",
                "--option",
                "ro",
                "--vfs-cache-mode=off",
                "--buffer-size=0",
                "--dir-cache-time=1s",
                "--poll-interval=0",
                "--file-perms=0444",
                "--dir-perms=0555",
                "--devname",
                device_name(config),
                "--contimeout=2s",
                "--timeout=5s",
                "--retries=1",
                "--low-level-retries=1",
                "--log-level=ERROR",
            ]
            process = subprocess.Popen(
                argv,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=clean_environment(),
                start_new_session=True,
            )
            deadline = time.monotonic() + 15
            while not mounted_view(settings):
                if process.poll() is not None or time.monotonic() >= deadline:
                    raise StorageError("Read-only experimental view failed to mount")
                time.sleep(0.05)
            current = mounted_view(settings)
            if (
                current["source"] != device_name(config)
                or "ro" not in current["options"]
                or current["type"] != "fuse.rclone"
            ):
                raise StorageError("Experimental mount did not establish read-only policy")
            atomic_json(
                config.state_path.parent / "view.json",
                {
                    "mount_id": current["id"],
                    "storage": str(config.storage_root),
                    "volume": config.volume_id,
                    "offline": offline,
                },
            )
        if ready:
            ready()
        while process.poll() is None and not stop():
            time.sleep(0.1)
        if process.poll() is not None and process.returncode != 0:
            raise StorageError("Experimental read-only view exited")
    finally:
        # Only unmount the exact view this process created. A normal unmount
        # may refuse busy handles; no lazy/forced unmount or daily mount action.
        if process and mounted_view(settings) and mounted_view(settings)["source"] == device_name(config):
            result = subprocess.run(["fusermount3", "-u", str(view)], capture_output=True, timeout=10)
            if result.returncode:
                raise StorageError("Experimental view is busy; close its open files and unmount it normally")
        if process and process.poll() is None:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.send_signal(signal.SIGTERM)
                process.wait(timeout=5)
        if runtime_conf and not mounted_view(settings):
            runtime_conf.unlink(missing_ok=True)
