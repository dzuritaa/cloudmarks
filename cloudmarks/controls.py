"""Explicit, cancellable local controls; the status socket remains read-only."""

import errno
import fcntl
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

from .backend import create as create_backend
from .install import atomic_json
from .ipc import load_settings, private_directory, read_private_json
from .limits import TREE_ENTRIES
from .model import Cancelled, ConfigurationError, IntegrityError, ScopeError, StorageError
from .offline import OfflineController
from .offline_config import load_offline, mounted_view
from .paths import SecureRoot, relative_parts
from .state import ReadStore, StateStore
from .tracking import ChangeTracker, manifest
from .view import attest_view


def profile(settings):
    if settings.get("version") != 4 or settings.get("mode") != "onedrive":
        raise ConfigurationError("Offline menu controls require version 4 account settings")
    value = read_private_json(settings["control_config"])
    if (
        not isinstance(value, dict)
        or set(value) - {"live_updates"} != {"version", "view_service"}
        or type(value.get("live_updates", False)) is not bool
        or type(value["version"]) is not int
        or value["version"] != 1
        or not isinstance(value["view_service"], str)
        or not re.fullmatch(r"cloudmarks-(?:[A-Za-z0-9_.@-]+-)?view\.service", value["view_service"])
    ):
        raise ConfigurationError("Control profile must name one explicit CloudMarks user view service")
    config, offline = load_offline(settings["core_config"])
    if offline["visible_root"] != settings["visible_root"]:
        raise ConfigurationError("Control and desktop view roots disagree")
    if settings["control_binding"] != binding(config, settings, value):
        raise ConfigurationError(
            "Offline control binding changed; reload the configured profile before retrying"
        )
    return config, offline, value


def binding(config, settings, value):
    fields = [
        config.expected_drive_id,
        config.allowed_root_id,
        config.volume_id,
        str(config.storage_root),
        str(config.state_path),
        settings["visible_root"],
        settings["upload_root"],
        value["view_service"],
    ]
    return hashlib.sha256(json.dumps(fields, ensure_ascii=True).encode()).hexdigest()


def progress_path(config):
    return config.state_path.parent / "control-progress.json"


def process_start(pid):
    try:
        return Path(f"/proc/{int(pid)}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, ValueError, IndexError):
        return None


class Progress:
    def __init__(self, config, path, operation, emit=lambda value: None):
        self.config, self.emit = config, emit
        self.value: dict = {
            "version": 1,
            "path": path,
            "operation": operation,
            "pid": os.getpid(),
            "start": process_start(os.getpid()),
            "phase": "planning",
            "completed": 0,
            "total": 0,
            "active": True,
        }
        self.last_emit = 0.0

    def update(self, **fields):
        changed = fields.get("phase", self.value["phase"]) != self.value["phase"]
        self.value.update(fields, updated_at=time.time())
        now = time.monotonic()
        if not changed and self.value["active"] and now - self.last_emit < 0.2:
            return
        self.last_emit = now
        target = progress_path(self.config)
        # A cancelled prior process may leave the temporary file, never unlink
        # an arbitrary symlink. The serialized control lock owns this pathname.
        temporary = target.with_name(target.name + ".new")
        if temporary.exists():
            if temporary.is_symlink() or not temporary.is_file():
                raise StorageError("Control progress temporary file requires review")
            temporary.unlink()
        atomic_json(target, self.value)
        self.emit(dict(self.value))


def view_holders(root, limit=5):
    """Names of this user's processes with files or a working directory in the view."""
    root = str(Path(root))
    names = set()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid != os.getuid():
                continue
            links = [entry / "cwd", *(entry / "fd").iterdir()]
            for link in links:
                target = os.readlink(link)
                if target == root or target.startswith(root + "/"):
                    names.add((entry / "comm").read_text().strip() or "unknown")
                    break
        except OSError:
            continue
    return sorted(names)[:limit]


def busy_message(root):
    names = view_holders(root)
    if names:
        return "Close " + ", ".join(names) + " (using the offline view), then click Retry"
    return "Close Files windows and applications using the Offline folder, then click Retry"


class LiveView:
    """Keep the verified read-only view mounted while copies change underneath it.

    Publication and eviction are atomic renames inside the store; the view's
    one-second directory cache shows them without a remount, and open readers
    keep their original bytes (spikes/live-view). A foreign mount is refused.
    """

    def __init__(self, config, settings):
        self.config, self.settings = config, settings

    def was_active(self):
        return False

    def stop(self):
        live_guard(self.config)(self.settings)
        return True

    def start(self):
        return None


def live_guard(config):
    def guard(settings):
        if mounted_view(settings) and not attest_view(config, settings):
            raise ScopeError("Another mount occupies the offline view; preserve it and check the profile")

    return guard


class ServiceView:
    def __init__(self, config, settings, unit, *, command=subprocess.run):
        self.config, self.settings, self.unit, self.command = config, settings, unit, command

    def was_active(self):
        result = self.command(
            ["systemctl", "--user", "show", "--property=ActiveState", "--value", self.unit],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0 and result.stdout.strip() in ("active", "activating", "reloading")

    def stop(self):
        if mounted_view(self.settings) and not attest_view(self.config, self.settings):
            raise ScopeError("Another mount occupies the offline view; preserve it and check the profile")
        try:
            self.command(["systemctl", "--user", "stop", self.unit], capture_output=True, timeout=35)
        except subprocess.TimeoutExpired:
            pass
        if mounted_view(self.settings):
            if not attest_view(self.config, self.settings):
                raise ScopeError("Offline mount changed while stopping; preserve it")
            self.command(
                ["fusermount3", "-u", self.settings["visible_root"]], capture_output=True, timeout=10
            )
        if mounted_view(self.settings):
            raise StorageError(busy_message(self.settings["visible_root"]))
        return True

    def start(self):
        if mounted_view(self.settings):
            if not attest_view(self.config, self.settings):
                raise ScopeError("Another mount occupies the offline view; no replacement was attempted")
            return
        result = self.command(["systemctl", "--user", "start", self.unit], capture_output=True, timeout=45)
        if result.returncode:
            raise StorageError("Offline view did not restart; open the CloudMarks launcher to retry")
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            try:
                if attest_view(self.config, self.settings):
                    return
            except (OSError, StorageError, ValueError):
                pass
            time.sleep(0.2)
        raise StorageError("Offline view is still starting; retry its launcher after checking the connection")


def reuse_current(tracker, store, observation, *, pin):
    """Skip a download when the retained copy already matches fresh OneDrive metadata.

    Keep would refuse or fail on the same preconditions checked here, so
    anything unusual falls through to the full keep path. Reuse requires a
    fresh describe, the same version as planned, both local copies hashing to
    the recorded SHA-256 and the server checksum matching the retained object.
    """
    remote = observation.remote
    row = store.row(remote.identity)
    entry = tracker.control.entry(remote.identity)
    if (
        row is None
        or row["dirty"]
        or row["operation"] != "idle"
        or entry is None
        or entry["path"] != remote.relative_path
        or store.issue(remote.identity)
    ):
        return False
    fresh = tracker.backend.describe(remote.relative_path)
    if not observation.same_version(fresh) or not tracker.current(fresh):
        return False
    if pin:
        store.set_pin(remote.identity, True)
    return True


def plan(config, offline, backend, path, operation, *, cancelled=lambda: False):
    relative_parts(path)
    root = backend.describe(path, allow_directory=True)
    if root.remote.is_directory:
        first = backend.tree(path, limit=TREE_ENTRIES, cancelled=cancelled)
        second = backend.tree(path, limit=TREE_ENTRIES, cancelled=cancelled)
        if manifest(first) != manifest(second):
            raise IntegrityError("Folder changed while planning; retry without changing local copies")
        observations = second
    else:
        observations = [root]
    with ReadStore(config) as store:
        intent = store.folder_intent(root.remote.identity)
        if operation == "free" and intent and json.loads(intent["manifest"]) != manifest(observations):
            raise ScopeError(
                "Cloud folder changed; refresh its offline selection before freeing local copies"
            )
        retained = 0
        for value in observations:
            remote = value.remote
            config.validate_remote(remote)
            if remote.relative_path != path and not remote.relative_path.startswith(path + "/"):
                raise ScopeError("Control plan escaped its selected path")
            issue = store.issue(remote.identity)
            row = store.row(remote.identity)
            retained += int(store.copy(remote.identity) is not None)
            if issue or row is not None and (row["dirty"] or row["operation"] == "active"):
                raise ScopeError(
                    "The selection contains changed or unresolved local data; preserve it and review"
                )
        total = sum(not v.remote.is_directory for v in observations)
    return (
        root,
        observations,
        {
            "operation": operation,
            "path": path,
            "directory": root.remote.is_directory,
            "files": total,
            "retained_files": retained,
            "bytes": sum(v.remote.size for v in observations),
            "cloud_writes": False,
            "requires_unmount": True,
        },
    )


@contextmanager
def release_intent(store, tracker, controller, root):
    """Release selection intent for a Free, restoring it unless the Free completes.

    The previous pins are committed to the ``releases`` journal before any pin
    is removed. An exception restores them immediately; abrupt process death
    leaves the journal row, and the next state writer restores it on open.
    """
    if root.is_directory:
        pins = store.pin_sources_owned("folder:" + root.identity.item_id)
        row = store.folder_intent(root.identity)
        intent = None if row is None else row["pinned"]
    else:
        pins = store.direct_pins(root.identity)
        intent = None
    store.begin_release(root.identity, pins, intent)
    try:
        if root.is_directory:
            if intent is not None:
                tracker.unpin_tree(root.relative_path)
        else:
            controller.unpin(root.relative_path)
        yield intent is not None
    except BaseException:
        store.restore_releases(root.identity)
        raise
    store.finish_release(root.identity)


@contextmanager
def control_lock(config):
    """Serialize control operations for one profile (separate from the state writer lock)."""
    parent = private_directory(config.state_path.parent)
    lock = os.open(
        "control.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=parent
    )
    os.close(parent)
    try:
        info = os.fstat(lock)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
            raise StorageError("Control lock is unsafe")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise StorageError("Another offline operation is running; finish or cancel it first") from None
        yield
    finally:
        os.close(lock)


def view_manager(config, offline, control_settings, *, live, service=None):
    if service:
        return service
    if live:
        return LiveView(config, offline)
    return ServiceView(config, offline, control_settings["view_service"])


class ControlRun:
    """The store-changing part of one Keep or Free, run under the state writer lock.

    ``plan`` has already described the selection twice and checked it against
    state. ``apply`` re-checks everything it relies on, because the cloud and
    the store can change between planning and taking the locks.
    """

    def __init__(self, config, offline, backend, root, observations, summary, *, guard, cancelled, progress):
        self.config, self.offline, self.backend = config, offline, backend
        self.root, self.observations, self.summary = root, observations, summary
        self.guard, self.cancelled, self.progress = guard, cancelled, progress
        self.operation = summary["operation"]
        self.done = self.skipped = 0

    def controller(self, store):
        phase = "downloading" if self.operation == "keep" else "freeing"
        return OfflineController(
            self.config,
            store,
            self.backend,
            self.offline,
            **self.guard,
            cancelled=self.cancelled,
            progress=lambda **v: self.progress.update(
                phase=phase,
                completed=self.done,
                bytes_done=v.get("completed", 0),
                bytes_total=v.get("total", 0),
            ),
        )

    def apply(self):
        with StateStore(self.config) as store:
            controller = self.controller(store)
            tracker = ChangeTracker(controller)
            controller.recover()
            if self.operation == "keep":
                self.keep(store, controller, tracker)
            else:
                self.audit_retained(controller)
                self.free(store, controller, tracker)

    def keep(self, store, controller, tracker):
        path, directory = self.root.remote.relative_path, self.root.remote.is_directory
        if directory:
            self.observations = tracker.queue_tree(path)
            self.summary["files"] = sum(not v.remote.is_directory for v in self.observations)
            self.progress.update(total=self.summary["files"])
            self.create_folders()
        else:
            tracker.queue(path)
        # Existing unrelated pending jobs are not consumed by a UI selection.
        for value in self.observations:
            if value.remote.is_directory:
                continue
            controller.check_cancelled()
            self.progress.update(phase="downloading", completed=self.done, total=self.summary["files"])
            if not reuse_current(tracker, store, value, pin=not directory):
                controller.keep(value.remote.relative_path, pin=not directory)
            store.finish_job(value.remote.identity)
            self.done += 1
            self.progress.update(completed=self.done)

    def create_folders(self):
        """Empty cloud folders are part of a kept folder: create them in the visible tree."""
        with SecureRoot(self.config.storage_root, self.config.volume_id) as disk:
            for value in self.observations:
                if value.remote.is_directory:
                    with disk.parent("local/" + value.remote.relative_path + "/.directory", create=True):
                        pass

    def audit_retained(self, controller):
        """Check every retained copy against the planned cloud version before releasing any pin.

        Free still repeats its own fresh checksum and version checks
        immediately before each eviction.
        """
        with SecureRoot(self.config.storage_root, self.config.volume_id) as disk:
            for value in self.observations:
                entry = controller.entry(value.remote.identity)
                if entry:
                    controller.verify_entry(disk, entry)
                    if (entry["copy"]["revision"], entry["copy"]["size"]) != (
                        value.remote.revision,
                        value.remote.size,
                    ):
                        raise IntegrityError(
                            "Cloud version differs; preserve the offline copy and refresh it first"
                        )

    def free(self, store, controller, tracker):
        with release_intent(store, tracker, controller, self.root.remote) as had_folder_intent:
            for value in self.observations:
                controller.check_cancelled()
                if value.remote.is_directory or not controller.entry(value.remote.identity):
                    continue
                if store.row(value.remote.identity)["pinned"]:
                    self.skipped += 1  # Another selection still keeps this file.
                    continue
                self.progress.update(phase="freeing", completed=self.done)
                controller.free(value.remote.relative_path)
                self.done += 1
                self.progress.update(completed=self.done)
            if self.root.remote.is_directory and had_folder_intent:
                self.remove_empty_folders(store)

    def remove_empty_folders(self, store):
        """Deepest first; a folder still holding retained files (ENOTEMPTY) stays."""
        with SecureRoot(self.config.storage_root, self.config.volume_id) as disk:
            for value in sorted(self.observations, key=lambda v: len(v.remote.relative_path), reverse=True):
                if not value.remote.is_directory or store.row(value.remote.identity)["pinned"]:
                    continue
                try:
                    with disk.parent("local/" + value.remote.relative_path) as (fd, name):
                        os.rmdir(name, dir_fd=fd)
                        os.fsync(fd)
                except OSError as exc:
                    if exc.errno not in (errno.ENOENT, errno.ENOTEMPTY):
                        raise

    def verify_published(self):
        """After Keep, prove each file through the mounted read-only view."""
        from .verification import verify

        with StateStore(self.config) as store:
            controller = OfflineController(
                self.config, store, self.backend, self.offline, **self.guard, cancelled=self.cancelled
            )
            files = [v for v in self.observations if not v.remote.is_directory]
            for index, value in enumerate(files):
                controller.check_cancelled()
                self.progress.update(phase="verifying", completed=index, total=self.summary["files"])
                verify(controller, value.remote.relative_path)


def settle(manager, offline, progress, *, was_mounted, error):
    """Restore a view that was mounted before the operation, and report how it ended."""
    if was_mounted and not mounted_view(offline):
        try:
            manager.start()
        except Exception:
            progress.update(
                phase="error",
                active=False,
                reason="Offline view needs attention; retry its launcher. Existing files are preserved",
            )
            if error is None:
                raise
    if error is not None:
        progress.update(
            phase="cancelled" if isinstance(error, Cancelled) else "error",
            active=False,
            reason=str(error)
            if isinstance(error, (ScopeError, StorageError, Cancelled))
            else "Offline operation failed; previous data is preserved",
        )


def execute(
    settings,
    path,
    operation,
    *,
    dry_run=False,
    cancelled=lambda: False,
    emit=lambda value: None,
    backend=None,
    service=None,
    expected_binding=None,
):
    """Keep or free one exact file or complete folder from the desktop menu.

    Stages: validate the profile → plan (read-only) → take the control lock →
    stop or guard the view → apply under the state writer lock → restore the
    view → verify kept files through it → report.
    """
    if operation not in ("keep", "free"):
        raise ConfigurationError("Choose keep or free")
    config, offline, control_settings = profile(settings)
    if expected_binding is not None and expected_binding != settings["control_binding"]:
        raise ScopeError("This selection belongs to an older profile; reopen its folder and retry")
    backend = backend or create_backend(config, offline)
    root, observations, summary = plan(config, offline, backend, path, operation, cancelled=cancelled)
    live = control_settings.get("live_updates", False)
    summary["requires_unmount"] = not live
    if dry_run:
        return dict(summary, dry_run=True)
    if operation == "free" and not summary["retained_files"] and not root.remote.is_directory:
        return dict(summary, completed=0, already_cloud_only=True)
    with control_lock(config):
        progress = Progress(config, path, operation, emit)
        progress.update(total=summary["files"], phase="waiting")
        manager = view_manager(config, offline, control_settings, live=live, service=service)
        was_mounted = bool(mounted_view(offline)) or (hasattr(manager, "was_active") and manager.was_active())
        run = ControlRun(
            config,
            offline,
            backend,
            root,
            observations,
            summary,
            guard={"unmounted": live_guard(config)} if live else {},
            cancelled=cancelled,
            progress=progress,
        )
        error = None
        try:
            if cancelled():
                raise Cancelled("Offline operation cancelled; previous data is preserved")
            manager.stop()
            run.apply()
            if was_mounted:
                progress.update(phase="restoring")
                manager.start()
            if operation == "keep" and mounted_view(offline):
                run.verify_published()
            progress.update(phase="complete", completed=run.done, active=False, skipped=run.skipped)
            return dict(summary, completed=run.done, retained_by_other_pins=run.skipped)
        except BaseException as exc:
            error = exc
            raise
        finally:
            settle(manager, offline, progress, was_mounted=was_mounted, error=error)


def configure(commands):
    parser = commands.add_parser(
        "control", help="Keep/free an exact file or complete folder in the configured offline view"
    )
    parser.add_argument("--settings", required=True, type=Path)
    parser.add_argument("--operation", choices=("keep", "free"), required=True)
    parser.add_argument(
        "--path", required=True, help="Exact relative file/folder path inside the allowed cloud root"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and describe the selection without changing state, mounts or bytes",
    )
    parser.add_argument("--binding", help="Expected configuration fingerprint, supplied by the desktop menu")


def run(args):
    stopped = [False]
    previous = signal.signal(signal.SIGINT, lambda *unused: stopped.__setitem__(0, True))
    try:
        result = execute(
            load_settings(args.settings),
            args.path,
            args.operation,
            dry_run=args.dry_run,
            cancelled=lambda: stopped[0],
            emit=lambda value: print(json.dumps(value), flush=True),
            expected_binding=args.binding,
        )
        print(json.dumps(dict(result, result=True)), flush=True)
    finally:
        signal.signal(signal.SIGINT, previous)
