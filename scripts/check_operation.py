"""Verify deployment and diagnostics in a temporary HOME, with no desktop/account.

    python -m scripts.check_operation

Uses fake rclone/systemctl executables. systemd-analyze verifies generated units
without starting them; absent tooling is reported explicitly. No setup runs.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from cloudmarks.fixture_session import create
from cloudmarks.install import inventory
from cloudmarks.ipc import load_settings
from cloudmarks.setup import render_units


def cli(*args):
    result = subprocess.run(
        [sys.executable, "-I", "-m", "cloudmarks", *map(str, args)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode:
        raise RuntimeError(
            f"cloudmarks {args[0]} failed: {result.stderr.strip()}; inspect the temporary fixture"
        )
    return result.stdout


def verify_units(base, paths):
    analyzer = shutil.which("systemd-analyze")
    if not analyzer or not shutil.which("fusermount3"):
        return "skipped: install systemd and fuse3 to verify generated user units"
    units = base / "units"
    units.mkdir()
    names = []
    for name, contents in render_units(paths, python=sys.executable, rclone=paths["rclone"]).items():
        target = units / name
        target.write_text(contents)
        names.append(str(target))
    result = subprocess.run(
        [analyzer, "--user", "verify", "--man=no", "--generators=no", *names],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode:
        raise RuntimeError(
            "Generated units failed systemd-analyze --user verify; inspect render_units: "
            + result.stderr.strip()
        )
    return "passed: systemd-analyze --user verify (no services started)"


def check(*, run_cli=cli):
    with tempfile.TemporaryDirectory(prefix="cm-operation-", dir="/tmp") as temporary:
        base = Path(temporary)
        environment = {
            "HOME": str(base),
            "PATH": str(base / "bin") + os.pathsep + os.defpath,
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=" + str(base / "absent-bus"),
            "SYSTEMD_UNIT_PATH": str(base / "units") + ":/usr/lib/systemd/user:/lib/systemd/user",
        }
        for key, subdir in [
            ("XDG_CONFIG_HOME", "config"),
            ("XDG_DATA_HOME", "data"),
            ("XDG_STATE_HOME", "state"),
            ("XDG_CACHE_HOME", "cache"),
            ("XDG_RUNTIME_DIR", "run"),
        ]:
            target = base / subdir
            target.mkdir(mode=0o700)
            environment[key] = str(target)
        binaries = base / "bin"
        binaries.mkdir()
        for name, body in {
            "rclone": 'test "$1" = version || exit 99\necho "rclone v1.75.2"',
            "systemctl": 'test "$1 $2 $3" = "--user show --property=LoadState,ActiveState" || exit 99\necho LoadState=not-found\necho ActiveState=inactive',
        }.items():
            executable = binaries / name
            executable.write_text("#!/bin/sh\n" + body + "\n")
            executable.chmod(0o700)
        with patch.dict(os.environ, environment):
            settings, store = create(base / "fixture")
            store.close()
            source = base / "fixture/desktop.json"
            preserved = store.config.state_path.read_bytes()
            unrelated = base / "data/keep.txt"
            unrelated.write_text("unrelated")
            run_cli("install", "--settings", source)
            value, config, root, extension, desktop = inventory()
            if load_settings(desktop)["enabled"]:
                raise AssertionError("Installation must start disabled")
            run_cli("enable")
            if not load_settings(desktop)["enabled"]:
                raise AssertionError("Enable did not update private desktop settings")
            report = json.loads(run_cli("doctor", "--json"))
            checks = {item["name"]: item for item in report["checks"]}
            if checks["Installed files"]["status"] != "ok" or checks["OneDrive profile"]["status"] != "skip":
                raise AssertionError("Doctor must verify deployment and skip account checks for the fixture")
            run_cli("disable")
            if load_settings(desktop)["enabled"]:
                raise AssertionError("Disable did not clear badge enablement")
            paths = {
                "desktop": str(desktop),
                "helper": str(root / "run.py"),
                "offline": str(base / "offline.json"),
                "mount_env": str(base / "mount.env"),
                "upload_root": settings["visible_root"],
                "upload_config": str(base / "upload.conf"),
                "upload_cache": str(base / "cache/upload"),
                "upload_socket": str(base / "run/upload.sock"),
                "reserve_gib": 1,
                "rclone": str(binaries / "rclone"),
            }
            unit_result = verify_units(base, paths)
            run_cli("uninstall")
            if root.exists() or extension.exists() or desktop.exists() or (config / "install.json").exists():
                raise AssertionError("Uninstall left owned deployment files; inspect the ownership manifest")
            if not unrelated.exists() or store.config.state_path.read_bytes() != preserved:
                raise AssertionError("Deployment round trip modified unrelated files or fixture state")
            # A second installation proves cleanup did not strand a deployment.
            run_cli("install", "--settings", source)
            run_cli("uninstall")
            return {
                "round_trip": "passed",
                "doctor": "passed (fixture, offline)",
                "units": unit_result,
                "owned_files": len(value["files"]),
            }


if __name__ == "__main__":
    print(json.dumps(check(), indent=2))
