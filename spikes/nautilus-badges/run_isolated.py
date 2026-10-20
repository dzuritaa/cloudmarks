#!/usr/bin/env python3
"""Run real Nautilus in an isolated Xvfb/DBus session without installing it.

Ubuntu packages must be downloaded/extracted first; see README.md. No sudo,
daily desktop restart, cloud access, or real home-directory access is used.
"""

import argparse
import fcntl
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent


def command(*args, **kwargs):
    return subprocess.run(args, check=True, timeout=30, **kwargs)


def inside(args):
    import gi
    from PIL import ImageGrab

    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    work = Path(args.work)

    def capture(path):
        windows = subprocess.check_output(["xwininfo", "-root", "-tree"], text=True)
        for line in windows.splitlines():
            if '"CloudMarks badge fixtures"' in line:
                match = re.search(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)", line)
                if match:
                    width, height, x, y = map(int, match.groups())
                    # Capture the fixture content pane only. Unix volume
                    # monitors can show host disk labels even on a private bus.
                    ImageGrab.grab(
                        bbox=(x + 240, y, x + width, y + height), xdisplay=os.environ["DISPLAY"]
                    ).save(path)
                    return
        raise RuntimeError("Cannot find the isolated fixture window for capture")

    fixtures = work / "CloudMarks badge fixtures"
    fixtures.mkdir(exist_ok=True)
    for name in (
        "01-cloud.txt",
        "02-local.txt",
        "03-transfer.txt",
        "04-error.txt",
        "05-unknown.txt",
        "06-refresh.txt",
    ):
        (fixtures / name).write_text("Disposable CloudMarks presentation fixture.\n")
    # A neighbour deliberately has no badge.
    (fixtures / "07-unmanaged.txt").write_text("Not in the spike's hard-coded state map.\n")
    extensions = work / "data/nautilus-python/extensions"
    extensions.mkdir(parents=True, exist_ok=True)
    installed = extensions / "cloudmarks_badge_spike.py"
    shutil.copy2(HERE / installed.name, installed)

    command("gsettings", "set", "org.gnome.desktop.interface", "icon-theme", "Yaru")
    command("gsettings", "set", "org.gnome.nautilus.preferences", "show-image-thumbnails", "never")
    command("gsettings", "set", "org.gnome.nautilus.window-state", "initial-size", "(1180, 600)")
    command(
        "gsettings",
        "set",
        "org.gnome.nautilus.list-view",
        "default-visible-columns",
        "['name', 'CloudMarksSpike::status', 'size']",
    )
    command(
        "gsettings",
        "set",
        "org.gnome.nautilus.list-view",
        "default-column-order",
        "['name', 'CloudMarksSpike::status', 'size']",
    )
    from gi.repository import Gdk

    Gtk.init_check()
    display = Gdk.Display.get_default() or Gdk.Display.open(os.environ["DISPLAY"])
    if display is None:
        diagnostic = subprocess.run(["xdpyinfo"], capture_output=True, text=True)
        raise RuntimeError(
            f"GTK cannot open isolated display {os.environ.get('DISPLAY')}: {diagnostic.stderr}"
        )
    Gtk.Settings.get_default().set_property("gtk-icon-theme-name", "Yaru")
    theme = Gtk.IconTheme.get_for_display(display)
    icons = [
        "emblem-downloads",
        "emblem-default",
        "emblem-synchronizing",
        "emblem-important",
        "emblem-dialog-question",
        "emblem-urgent",
        "emblem-readonly",
    ]
    (work / "icon-availability.json").write_text(
        json.dumps({name: theme.has_icon(name) for name in icons}, indent=2) + "\n"
    )
    captures = work / "captures"
    captures.mkdir(exist_ok=True)
    log = work / "callbacks.jsonl"
    if log.exists():
        log.unlink()
    results = []
    for view in ("icon", "list"):
        for zoom in ("small", "large"):
            for style in ("light", "dark"):
                name = f"{view}-{zoom}-{style}"
                command(
                    "gsettings",
                    "set",
                    "org.gnome.nautilus.preferences",
                    "default-folder-viewer",
                    f"{view}-view",
                )
                command("gsettings", "set", f"org.gnome.nautilus.{view}-view", "default-zoom-level", zoom)
                env = os.environ | {
                    "ADW_DEBUG_COLOR_SCHEME": f"prefer-{style}",
                    "CLOUDMARKS_SPIKE_ROOT": str(fixtures),
                    "CLOUDMARKS_SPIKE_LOG": str(log),
                }
                with (work / f"{name}.stderr").open("w") as stderr:
                    app = subprocess.Popen(
                        ["nautilus", "--new-window", str(fixtures)], env=env, stdout=stderr, stderr=stderr
                    )
                    try:
                        time.sleep(3)
                        if app.poll() is not None:
                            raise RuntimeError(f"Nautilus exited during {name}")
                        capture(captures / f"{name}.png")
                        events = [json.loads(line) for line in log.read_text().splitlines()]
                        results.append(
                            {
                                "case": name,
                                "completion_count": sum(e["event"] == "completed" for e in events),
                                "alive_at_capture": True,
                            }
                        )
                    finally:
                        # New DBus session: this cannot reach the daily Files instance.
                        subprocess.run(["nautilus", "--quit"], env=env, capture_output=True, timeout=10)
                        try:
                            app.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            app.terminate()
                            app.wait(timeout=10)
                print(name, "captured", flush=True)
    # Verify disable/uninstall before leaving; removal affects only the sandbox.
    installed.unlink()
    env = os.environ | {"ADW_DEBUG_COLOR_SCHEME": "prefer-light"}
    with (work / "uninstalled.stderr").open("w") as stderr:
        app = subprocess.Popen(
            ["nautilus", "--new-window", str(fixtures)], env=env, stdout=stderr, stderr=stderr
        )
        try:
            before = log.stat().st_size
            time.sleep(2)
            capture(captures / "uninstalled.png")
            assert log.stat().st_size == before, "Extension still ran after removal"
        finally:
            subprocess.run(["nautilus", "--quit"], capture_output=True, timeout=10)
            app.wait(timeout=10)
    (work / "results.json").write_text(
        json.dumps({"cases": results, "uninstall_no_callbacks": True}, indent=2) + "\n"
    )


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
