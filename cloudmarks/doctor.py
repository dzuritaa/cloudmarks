"""Read-only installation and profile checks whose report is safe to share.

Checks never print paths, account names, IDs, emails or tokens. Network access
happens only with ``online=True``. No check mounts, unmounts, downloads, starts
services or writes files.
"""

import glob
import json
import os
import re
import shutil
import socket
import stat
import subprocess
from pathlib import Path

from .model import CloudMarksError

TESTED_RCLONE = (1, 75, 2)
UNITS = ("cloudmarks-upload.service", "cloudmarks-status.service", "cloudmarks-view.service")
SYMBOLS = {"ok": "PASS", "warn": "WARN", "fail": "FAIL", "skip": "SKIP"}
LIBRARY_ROOTS = ("/usr/lib", "/usr/lib64", "/usr/local/lib")


class Report:
    def __init__(self):
        self.checks = []

    def add(self, group, name, status, detail, hint=None):
        if status not in SYMBOLS:
            raise ValueError(status)
        self.checks.append({"group": group, "name": name, "status": status, "detail": detail, "hint": hint})
        return status

    def attempt(self, group, name, function, hint=None):
        """Run one check; expected CloudMarks errors carry sanitized messages."""
        try:
            result = function()
        except CloudMarksError as error:
            return self.add(group, name, "fail", str(error), hint)
        except Exception as error:
            return self.add(group, name, "fail", f"Unexpected {type(error).__name__}", hint)
        if isinstance(result, tuple):
            return self.add(group, name, *result)
        return self.add(group, name, "ok", result or "OK")

    @property
    def failed(self):
        return any(check["status"] == "fail" for check in self.checks)

    def as_json(self):
        counts = {status: 0 for status in SYMBOLS}
        for check in self.checks:
            counts[check["status"]] += 1
        return {"version": 1, "ok": not self.failed, "counts": counts, "checks": self.checks}

    def render(self):
        lines, group = [], None
        for check in self.checks:
            if check["group"] != group:
                group = check["group"]
                lines.append(("" if not lines else "\n") + group)
            lines.append(f"  [{SYMBOLS[check['status']]}] {check['name']}: {check['detail']}")
            if check["hint"] and check["status"] in ("warn", "fail"):
                lines.append(f"         -> {check['hint']}")
        summary = self.as_json()["counts"]
        lines.append(
            f"\n{summary['fail']} failed, {summary['warn']} warnings, {summary['ok']} passed, "
            f"{summary['skip']} skipped. This report contains no paths, IDs or credentials."
        )
        return "\n".join(lines)


def rclone_version(executable, run=subprocess.run):
    result = run([executable, "version"], capture_output=True, text=True, timeout=10)
    match = re.match(r"rclone v(\d+)\.(\d+)\.(\d+)", result.stdout or "")
    if result.returncode or not match:
        raise ValueError("Unrecognized rclone version output")
    return tuple(int(part) for part in match.groups())


def find_library(pattern, roots=LIBRARY_ROOTS):
    return any(glob.glob(os.path.join(root, pattern)) for root in roots)


def system_checks(report, *, rclone=None, which=shutil.which, run=subprocess.run, roots=LIBRARY_ROOTS):
    group = "System"
    executable = rclone or which("rclone")
    if not executable or not os.access(executable, os.X_OK):
        report.add(group, "rclone", "fail", "Not found", "Install rclone 1.75 or newer (rclone.org/install)")
    else:
        try:
            version = rclone_version(executable, run)
        except (OSError, ValueError, subprocess.SubprocessError):
            report.add(group, "rclone", "fail", "Found, but its version could not be read")
        else:
            shown = "v" + ".".join(map(str, version))
            if version[:2] < TESTED_RCLONE[:2]:
                report.add(
                    group, "rclone", "warn", f"{shown} is older than the tested v1.75", "Upgrade rclone"
                )
            elif version[:2] > TESTED_RCLONE[:2]:
                report.add(group, "rclone", "warn", f"{shown} is newer than the tested v1.75")
            else:
                report.add(group, "rclone", "ok", shown)
    report.add(
        group,
        "FUSE",
        "ok" if which("fusermount3") else "fail",
        "fusermount3 available" if which("fusermount3") else "fusermount3 not found",
        "Install the fuse3 package",
    )
    loader = find_library("**/nautilus/extensions-4/libnautilus-python.so", roots) or find_library(
        "nautilus/extensions-4/libnautilus-python.so", roots
    )
    report.add(
        group,
        "Nautilus Python loader",
        "ok" if loader else "warn",
        "Installed" if loader else "Not found; badges and menus cannot load",
        "Install python3-nautilus (Ubuntu/Debian) or nautilus-python (Fedora/Arch)",
    )
    typelib = find_library("*/girepository-1.0/Nautilus-4.*.typelib", roots) or find_library(
        "girepository-1.0/Nautilus-4.*.typelib", roots
    )
    report.add(
        group,
        "Nautilus introspection",
        "ok" if typelib else "warn",
        "Nautilus 4.x typelib installed" if typelib else "Nautilus 4.x typelib not found",
        "Install gir1.2-nautilus-4.1 (Ubuntu/Debian)",
    )
    gtk = find_library("*/girepository-1.0/Gtk-4.0.typelib", roots) or find_library(
        "girepository-1.0/Gtk-4.0.typelib", roots
    )
    report.add(
        group,
        "GTK 4 introspection",
        "ok" if gtk else "warn",
        "Installed" if gtk else "Not found; the progress window cannot open",
        "Install gir1.2-gtk-4.0 (Ubuntu/Debian)",
    )
    report.add(
        group,
        "systemd user services",
        "ok" if which("systemctl") else "warn",
        "systemctl available" if which("systemctl") else "systemctl not found; services must be run by hand",
    )


def unit_state(unit, run=subprocess.run):
    result = run(
        ["systemctl", "--user", "show", "--property=LoadState,ActiveState", unit],
        capture_output=True,
        text=True,
        timeout=5,
    )
    values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    return values.get("LoadState", "unknown"), values.get("ActiveState", "unknown")


def probe_helper(socket_path, timeout=0.6):
    from .ipc import TIMEOUT, VERSION, encode, private_directory, receive, same_user

    path = Path(socket_path)
    parent = private_directory(path.parent)
    try:
        info = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
    finally:
        os.close(parent)
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
        raise PermissionError("Helper socket is not a private socket owned by this user")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(min(timeout, TIMEOUT))
        connection.connect(str(path))
        if not same_user(connection):
            raise PermissionError("Helper belongs to another user")
        connection.sendall(encode({"version": VERSION, "op": "status", "paths": ["cloudmarks-doctor-probe"]}))
        response = receive(connection)
    if not isinstance(response, dict) or response.get("version") != VERSION:
        raise ValueError("Unexpected helper response")


def default_settings_path():
    from .install import locations

    return locations()[1] / "desktop.json"


def profile_checks(report, settings_path, *, online=False, run=subprocess.run):
    from .install import checked_contents, inventory
    from .ipc import load_settings

    group = "Installation"
    path = Path(settings_path) if settings_path else default_settings_path()
    if not path.exists():
        report.add(
            group,
            "Desktop settings",
            "warn",
            "CloudMarks is not installed for this user",
            "Run cloudmarks setup",
        )
        return
    settings = {}

    def load():
        settings.update(load_settings(path))
        return f"Version {settings['version']} ({settings['mode']})"

    if report.attempt(group, "Desktop settings", load) == "fail":
        return
    report.add(
        group,
        "Badges",
        "ok" if settings["enabled"] else "warn",
        "Enabled" if settings["enabled"] else "Disabled",
        "Run cloudmarks enable",
    )

    def adapter():
        value, *_, desktop = inventory()
        for name, checksum in value["files"].items():
            checked_contents(Path(name), checksum, mutable_settings=Path(name) == desktop)
        return f"{len(value['files'])} installed files match their checksums"

    if settings_path is None or path == default_settings_path():
        report.attempt(
            group, "Installed files", adapter, "Reinstall: cloudmarks uninstall, then cloudmarks install"
        )
    if settings["mode"] != "onedrive":
        report.add("Profile", "OneDrive profile", "skip", "Fixture settings have no account profile")
        return
    profile_group(report, settings, online=online, run=run)


def profile_group(report, settings, *, online, run):
    from .offline_config import check_volume, load_offline, mounted_view
    from .paths import SecureRoot

    group = "Profile"
    loaded = {}

    def offline():
        loaded["config"], loaded["offline"] = load_offline(settings["core_config"])
        if loaded["offline"]["visible_root"] != settings["visible_root"]:
            raise ValueError("Desktop and offline view folders disagree")
        blocked = len(loaded["config"].blocked_drive_ids)
        return f"Valid; {blocked} additional blocked drive(s)"

    if report.attempt(group, "Account and storage configuration", offline) == "fail":
        return
    config, values = loaded["config"], loaded["offline"]
    rclone = values["rclone"]
    report.add(
        group,
        "Configured rclone",
        "ok" if os.access(rclone, os.X_OK) else "fail",
        "Executable" if os.access(rclone, os.X_OK) else "Missing or not executable",
        "Reinstall rclone or update the profile",
    )

    def transport():
        from .onedrive import OneDrive

        OneDrive(config, values)
        return "Scoped to the configured drive and folder"

    report.attempt(group, "Transport configuration", transport)

    def storage():
        check_volume(config, values)
        if not config.storage_root.exists():
            return ("fail", "Store folder does not exist yet")
        with SecureRoot(config.storage_root, config.volume_id) as root:
            space = os.fstatvfs(root.fd)
            free = space.f_bavail * space.f_frsize
        if free < values["min_free_bytes"]:
            return ("warn", f"Free space ({free // 2**30} GiB) is below the configured reserve")
        return f"Disk and marker verified; {free // 2**30} GiB free"

    report.attempt(group, "Offline store", storage, "Connect the configured disk, or rerun cloudmarks setup")

    def view():
        from .view import attest_view

        if not mounted_view(values):
            return ("warn", "Not mounted")
        if not attest_view(config, values):
            return ("fail", "Another filesystem is mounted at the offline view")
        return "Mounted read-only and verified"

    report.attempt(group, "Offline view", view, "Start it: systemctl --user start cloudmarks-view.service")
    if settings["version"] in (3, 4):

        def upload():
            current = mounted_view({"visible_root": settings["upload_root"]})
            if not current:
                return ("warn", "Not mounted; files copied there will not upload")
            if current["type"] != "fuse.rclone":
                return ("fail", "Something other than rclone is mounted at the upload view")
            return "Mounted"

        report.attempt(
            group, "Upload view", upload, "Start it: systemctl --user start cloudmarks-upload.service"
        )

        def upload_config():
            from .model import canonical_drive
            from .onedrive import private_config

            parser = private_config(settings["upload_config"])
            section = parser["upload"] if parser.has_section("upload") else {}
            if (
                parser.sections() != ["upload"]
                or section.get("type") != "onedrive"
                or canonical_drive(section.get("drive_id")) != config.expected_drive_id
                or section.get("root_folder_id") != config.expected_drive_id + "#" + config.allowed_root_id
            ):
                raise ValueError("Upload remote does not match the configured drive and folder")
            return "Scoped to the configured drive and folder"

        report.attempt(group, "Upload configuration", upload_config)

        def upload_socket():
            try:
                info = os.stat(settings["upload_socket"], follow_symlinks=False)
            except FileNotFoundError:
                return ("warn", "Not present; the upload view is not running")
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise PermissionError("Upload control socket is not owned by this user")
            return "Private socket present"

        report.attempt(group, "Upload status socket", upload_socket, "Start the upload view service")

    def helper():
        try:
            probe_helper(settings["socket"])
        except (FileNotFoundError, ConnectionRefusedError):
            return ("warn", "Not running; badges show Needs checking")
        return "Responding"

    report.attempt(
        group, "Status helper", helper, "Start it: systemctl --user start cloudmarks-status.service"
    )
    if settings["version"] == 4:

        def controls():
            from .controls import profile

            _, _, value = profile(settings)
            return value

        control = {}

        def control_check():
            control.update(controls())
            return "Binding matches this profile"

        report.attempt(group, "Menu controls", control_check, "Rerun cloudmarks setup to rebind controls")
        if control.get("view_service") and shutil.which("systemctl"):
            try:
                load, active = unit_state(control["view_service"], run)
            except (OSError, subprocess.SubprocessError):
                load, active = "unknown", "unknown"
            report.add(
                group,
                "View service",
                "ok" if load == "loaded" else "fail",
                f"{load}, {active}",
                "Create the view service named in the control profile",
            )

    def store_health():
        from .diagnostics import doctor
        from .offline import OfflineController
        from .state import ReadStore

        with ReadStore(config) as store:
            health = doctor(OfflineController(config, store, None, values))
        issues = health["counts"].get("issues", 0)
        unfinished = health.get("unfinished_jobs", 0)
        detail = (
            f"schema {health['schema']}; {health['counts'].get('copies', 0)} offline copies, "
            f"{issues} issues, {unfinished} unfinished jobs"
        )
        return ("warn", detail) if issues or unfinished or health["attention_files"] else detail

    report.attempt(
        group, "Offline state", store_health, "Review: cloudmarks offline --config ... conflicts/jobs"
    )
    if online:

        def account():
            from .onedrive import OneDrive

            OneDrive(config, values).authenticate()
            return "Signed in; drive and folder match the profile"

        report.attempt("Online", "OneDrive account", account, "Sign in again with cloudmarks setup")


def services_checks(report, run=subprocess.run, which=shutil.which):
    if not which("systemctl"):
        return
    for unit in UNITS:
        try:
            load, active = unit_state(unit, run)
        except (OSError, subprocess.SubprocessError):
            report.add("Services", unit, "skip", "systemctl did not answer")
            continue
        if load == "not-found":
            report.add("Services", unit, "skip", "Not installed by cloudmarks setup")
        else:
            report.add(
                "Services",
                unit,
                "ok" if active == "active" else "warn",
                f"{load}, {active}",
                f"systemctl --user start {unit}",
            )


def run_doctor(settings_path=None, *, online=False, run=subprocess.run, which=shutil.which):
    report = Report()
    system_checks(report, run=run, which=which)
    profile_checks(report, settings_path, online=online, run=run)
    services_checks(report, run=run, which=which)
    return report


def main(args):
    report = run_doctor(args.settings, online=args.online)
    print(json.dumps(report.as_json(), indent=2) if args.json else report.render())
    return 1 if report.failed else 0
