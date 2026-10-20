#!/usr/bin/env python3
"""M2 acceptance test in real, isolated Files; no daily desktop changes.

The namespace/Xvfb harness follows the M0 test infrastructure. The installed
extension and helper are the M2 deployment, not the disposable M0 spike.
"""

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))


def command(*args, **kwargs):
    return subprocess.run(args, check=True, timeout=30, **kwargs)


def inside(args):
    import signal

    import gi
    from PIL import ImageGrab

    from cloudmarks import install
    from cloudmarks.ipc import request

    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    work = args.work
    captures = work / "captures"
    captures.mkdir(exist_ok=True)
    fixture = work / "fixture"
    observer_log = work / "observer.jsonl"
    observer_log.write_text("")
    results = {"fixture_only": True, "folder_size": 2000, "cases": []}
    helper_log = (work / "helper.stderr").open("w")
    helper = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "cloudmarks",
            "fixture-session",
            "--directory",
            str(fixture),
            "--count",
            "2000",
            "--cycle-seconds",
            "12",
        ],
        cwd=REPO,
        stdout=helper_log,
        stderr=helper_log,
    )
    app = None
    socket_path = fixture / "run/status.sock"
    try:
        deadline = time.monotonic() + 15
        while not socket_path.exists():
            if helper.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("Fixture helper failed to start")
            time.sleep(0.05)
        settings_path = fixture / "desktop.json"
        settings = json.loads(settings_path.read_text())
        view = Path(settings["visible_root"])
        # Required removal rehearsal precedes the first enablement.
        install.install(settings_path)
        _, _, _, extension, _ = install.inventory()
        install.uninstall()
        assert not extension.exists()
        results["uninstall_before_first_enable"] = True
        install.install(settings_path)
        install.set_enabled(True)
        command("gsettings", "set", "org.gnome.desktop.interface", "icon-theme", "Yaru")
        command("gsettings", "set", "org.gnome.nautilus.preferences", "show-image-thumbnails", "never")
        command("gsettings", "set", "org.gnome.nautilus.window-state", "initial-size", "(1180, 650)")
        for key in ("default-visible-columns", "default-column-order"):
            command(
                "gsettings",
                "set",
                "org.gnome.nautilus.list-view",
                key,
                "['name', 'CloudMarks::status', 'size']",
            )
        Gtk.init_check()
        Gtk.Settings.get_default().set_property("gtk-icon-theme-name", "Yaru")
        observer = extension.parent / "cloudmarks_test_observer.py"
        observer.write_text(OBSERVER)

        def events(case):
            return [
                value
                for line in observer_log.read_text().splitlines()
                if (value := json.loads(line)).get("case") == case
            ]

        def capture(name, title="CloudMarks fixtures"):
            windows = subprocess.check_output(["xwininfo", "-root", "-tree"], text=True)
            for line in windows.splitlines():
                if f'"{title}"' in line:
                    match = re.search(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)", line)
                    if match:
                        width, height, x, y = map(int, match.groups())
                        ImageGrab.grab(
                            bbox=(x + 240, y, x + width, y + height), xdisplay=os.environ["DISPLAY"]
                        ).save(captures / f"{name}.png")
                        return
            raise RuntimeError("Isolated fixture window missing")

        def launch(case, path=view, *, viewer="list", zoom="small", style="light"):
            command(
                "gsettings",
                "set",
                "org.gnome.nautilus.preferences",
                "default-folder-viewer",
                f"{viewer}-view",
            )
            command("gsettings", "set", f"org.gnome.nautilus.{viewer}-view", "default-zoom-level", zoom)
            env = os.environ | {
                "ADW_DEBUG_COLOR_SCHEME": f"prefer-{style}",
                "CM_CASE": case,
                "CM_LOG": str(observer_log),
                "CM_ROOT": str(work),
            }
            stderr = (work / f"{case}.stderr").open("w")
            process = subprocess.Popen(
                ["nautilus", "--new-window", str(path)], env=env, stdout=stderr, stderr=stderr
            )
            stderr.close()
            return process

        def quit_app(process):
            subprocess.run(["nautilus", "--quit"], capture_output=True, timeout=10)
            process.wait(timeout=10)

        # First launch verifies initial evidence, live pin transition, dead
        # helper fallback, and disable, all in the same Files process.
        app = launch("live")
        time.sleep(3)
        capture("initial")
        initial = request(
            socket_path,
            [
                "01-cloud.txt",
                "02-local.txt",
                "03-pinned.txt",
                "04-transfer.txt",
                "05-error.txt",
                "06-unknown.txt",
                "07-refresh.txt",
            ],
        )
        results["initial_states"] = {name: value["state"] for name, value in initial.items()}
        deadline = time.monotonic() + 15
        while request(socket_path, ["07-refresh.txt"])["07-refresh.txt"]["state"] != "kept_offline":
            if time.monotonic() > deadline:
                raise RuntimeError("Artificial fixture transition not observed")
            time.sleep(0.1)
        time.sleep(1.5)
        capture("refreshed")
        helper.send_signal(signal.SIGSTOP)
        time.sleep(2)
        capture("helper-unresponsive")
        stalled = [e for e in events("live") if e["event"] == "changed" and e["name"] == "02-local.txt"]
        assert stalled[-1]["label"].startswith("Needs checking"), "Hung helper retained green"
        helper.send_signal(signal.SIGCONT)
        time.sleep(2.5)
        capture("helper-recovered")
        recovered = [e for e in events("live") if e["event"] == "changed" and e["name"] == "02-local.txt"]
        assert recovered[-1]["label"].startswith("Saved locally"), "Helper recovery did not repaint"
        results["hung_helper_recovers_without_navigation"] = True
        helper.send_signal(signal.SIGINT)
        helper.wait(timeout=5)
        time.sleep(2)
        capture("helper-unavailable")
        install.set_enabled(False)
        time.sleep(1.2)
        capture("disabled")
        quit_app(app)
        app = None
        live = events("live")
        changes = [event for event in live if event["event"] == "changed"]

        def labels(name):
            return [event["label"] for event in changes if event["name"] == name]

        assert any(label.startswith("Saved locally") for label in labels("07-refresh.txt"))
        assert any(label.startswith("Kept offline") for label in labels("07-refresh.txt"))
        assert labels("02-local.txt")[-1] == "", "Disable did not clear the loaded adapter"
        assert any(label.startswith("Needs checking") for label in labels("02-local.txt"))
        results.update(
            live_transition_without_navigation=True,
            helper_loss_clears_green=True,
            disable_clears_loaded_extension=True,
        )

        # Restart uses persisted state: a formerly active transfer is an error.
        helper = subprocess.Popen(
            [sys.executable, "-m", "cloudmarks", "serve", "--settings", str(settings_path)],
            cwd=REPO,
            stdout=helper_log,
            stderr=helper_log,
        )
        deadline = time.monotonic() + 5
        while not socket_path.exists():
            if helper.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("Fixture helper restart failed")
            time.sleep(0.05)
        install.set_enabled(True)
        for viewer in ("icon", "list"):
            for zoom in ("small", "large"):
                for style in ("light", "dark"):
                    case = f"{viewer}-{zoom}-{style}"
                    app = launch(case, viewer=viewer, zoom=zoom, style=style)
                    time.sleep(2.5)
                    capture(case)
                    quit_app(app)
                    app = None
                    records = events(case)
                    assert any(
                        e["event"] == "changed" and e.get("label", "").startswith("Kept offline")
                        for e in records
                    ), f"No real helper status in {case}"
                    results["cases"].append(case)
                    print(case, "passed", flush=True)
        app = launch("large", view / "Large folder")
        time.sleep(5)
        capture("large-folder", "Large folder")
        quit_app(app)
        app = None
        records = events("large")
        heartbeat = [e for e in records if e["event"] == "heartbeat"]
        assert heartbeat and max(e["seen"] for e in heartbeat) >= 2000
        max_gap = max(e["max_gap_seconds"] for e in heartbeat)
        assert max_gap < 1, f"Files event loop stalled for {max_gap}s"
        results["large_folder"] = {
            "observed_entries": max(e["seen"] for e in heartbeat),
            "max_main_loop_gap_seconds": max_gap,
            "over_capacity_policy": "unknown; at most 512 tracked",
        }
        neighbour = work / "Unrelated fixtures"
        neighbour.mkdir()
        (neighbour / "unmanaged.txt").write_text("Outside the configured view.\n")
        app = launch("unrelated", neighbour)
        time.sleep(2)
        capture("unrelated", neighbour.name)
        quit_app(app)
        app = None
        assert all(not e.get("label") for e in events("unrelated") if e["event"] == "changed")
        results["unrelated_unbadged"] = True
        install.uninstall()
        assert not extension.exists()
        app = launch("uninstalled")
        time.sleep(2)
        capture("uninstalled")
        quit_app(app)
        app = None
        assert all(not e.get("label") for e in events("uninstalled") if e["event"] == "changed")
        results["uninstall_restores_baseline"] = True
        observer.unlink()
        (work / "results.json").write_text(json.dumps(results, indent=2) + "\n")
        print(json.dumps(results, indent=2), flush=True)
    finally:
        if app and app.poll() is None:
            subprocess.run(["nautilus", "--quit"], capture_output=True, timeout=10)
            app.wait(timeout=10)
        if helper.poll() is None:
            helper.send_signal(signal.SIGCONT)
            helper.send_signal(signal.SIGINT)
            helper.wait(timeout=5)
        helper_log.close()


OBSERVER = r'''"""Independent test observer; never changes badges or invalidates files."""
import json
import os
import time
import gi
gi.require_version("Nautilus", "4.1")
from gi.repository import GObject, GLib, Nautilus
class CloudMarksTestObserver(GObject.GObject, Nautilus.InfoProvider):
    def __init__(self):
        super().__init__()
        self.files = {}
        self.labels = {}
        self.last = time.monotonic()
        self.started = self.last
        self.gap = 0
        self.beats = 0
        GLib.timeout_add(100, self.scan)
    def update_file_info(self, file):
        if file.get_uri().startswith("file://" + os.environ["CM_ROOT"] + "/"):
            self.files[file.get_uri()] = file
    def log(self, event, **values):
        with open(os.environ["CM_LOG"], "a") as stream:
            stream.write(json.dumps(dict(case=os.environ["CM_CASE"], event=event,
                                         seconds=round(time.monotonic()-self.started, 3), **values))+"\n")
    def scan(self):
        now = time.monotonic()
        self.gap = max(self.gap, now-self.last)
        self.last = now
        for uri, file in list(self.files.items()):
            label = file.get_string_attribute("cloudmarks_status") or ""
            if uri not in self.labels or self.labels[uri] != label:
                self.labels[uri] = label
                self.log("changed", name=file.get_name(), label=label)
        self.beats += 1
        if self.beats % 5 == 0:
            self.log("heartbeat", seen=len(self.files), max_gap_seconds=round(self.gap, 4))
        return True
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sysroot", required=True, type=Path)
    parser.add_argument("--work", required=True, type=Path)
    parser.add_argument("--inside", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    args.sysroot = args.sysroot.resolve()
    args.work = args.work.resolve()
    if args.inside:
        inside(args)
        return
    args.work.mkdir(parents=True, exist_ok=True)
    lock = (args.work / ".run.lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        parser.error("A spike is already running in this work directory")
    for name in ("data", "config", "cache", "state", "runtime"):
        (args.work / name).mkdir(exist_ok=True, mode=0o700)
    # This new, network-isolated bus has no activated desktop services. Disable
    # only its AppArmor mediation: policy queries require a writable securityfs,
    # which the sandbox intentionally does not expose. Host policy is unchanged.
    bus_config = args.work / "session-bus.conf"
    bus_config.write_text("""<busconfig>
  <type>session</type><listen>unix:tmpdir=/tmp</listen><auth>EXTERNAL</auth>
  <apparmor mode="disabled"/>
  <policy context="default"><allow own="*"/><allow send_destination="*"/>
  <allow receive_sender="*"/></policy>
</busconfig>\n""")
    xvfb = args.sysroot / "usr/bin/Xvfb"
    extension_dir = args.sysroot / "usr/lib/x86_64-linux-gnu/nautilus/extensions-4"
    if not xvfb.exists() or not extension_dir.exists():
        parser.error("Extract the documented Ubuntu packages into --sysroot first")
    # Hide the actual home, runtime sockets and system extension directory. Only
    # the work directory and a private /tmp are writable; the host is read-only.
    env = os.environ | {
        "PATH": f"{args.sysroot}/usr/bin:/usr/bin:/bin",
        "XDG_DATA_HOME": str(args.work / "data"),
        "XDG_CONFIG_HOME": str(args.work / "config"),
        "XDG_CACHE_HOME": str(args.work / "cache"),
        "XDG_STATE_HOME": str(args.work / "state"),
        "XDG_RUNTIME_DIR": str(args.work / "runtime"),
        "GI_TYPELIB_PATH": str(args.sysroot / "usr/lib/x86_64-linux-gnu/girepository-1.0"),
        "GSETTINGS_BACKEND": "keyfile",
        "GDK_BACKEND": "x11",
        "GTK_A11Y": "none",
        "GSK_RENDERER": "cairo",
        "NO_AT_BRIDGE": "1",
        "GIO_USE_VOLUME_MONITOR": "unix",
    }
    env.pop("DBUS_SESSION_BUS_ADDRESS", None)
    env.pop("WAYLAND_DISPLAY", None)
    argv = [
        "bwrap",
        "--die-with-parent",
        "--unshare-net",
        "--ro-bind",
        "/",
        "/",
        "--tmpfs",
        str(Path.home()),
        "--tmpfs",
        "/run/user",
        "--tmpfs",
        "/run/mount",
        "--tmpfs",
        "/tmp",
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        "--ro-bind",
        "/dev/null",
        "/etc/fstab",
        "--ro-bind",
        str(args.sysroot),
        str(args.sysroot),
        "--bind",
        str(args.work),
        str(args.work),
        "--ro-bind",
        str(extension_dir),
        "/usr/lib/x86_64-linux-gnu/nautilus/extensions-4",
        "dbus-run-session",
        "--config-file",
        str(bus_config),
        "--",
        str(args.sysroot / "usr/bin/xvfb-run"),
        "-a",
        "-e",
        str(args.work / "xvfb.log"),
        "-s",
        "-screen 0 1280x800x24 -nolisten tcp -extension GLX",
        "/usr/bin/python3",
        str(Path(__file__).resolve()),
        "--inside",
        "--sysroot",
        str(args.sysroot),
        "--work",
        str(args.work),
    ]
    raise SystemExit(subprocess.run(argv, env=env).returncode)


if __name__ == "__main__":
    main()
