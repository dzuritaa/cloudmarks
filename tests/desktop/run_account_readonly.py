#!/usr/bin/env python3
"""Real M3 status through the unchanged read-only M2 adapter, in isolated Files.

Requires explicit private M3 config and one existing disposable root-level file.
Keeps that file using the CLI/controller, mounts a no-network offline view, and
mirrors it read-only into the desktop sandbox. Never writes/deletes cloud data.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from tests.desktop import run_isolated as sandbox  # noqa: E402 - select the checked-out project


def inside(args):
    import gi
    from PIL import ImageGrab

    from cloudmarks import install

    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    work = args.work
    alias = work / "CloudMarks read-only test"
    install.install(work / "mirror-settings.json")
    install.set_enabled(True)
    _, _, _, launcher, _ = install.inventory()
    observer = launcher.parent / "cloudmarks_account_observer.py"
    observer.write_text(OBSERVER)
    sandbox.command("gsettings", "set", "org.gnome.desktop.interface", "icon-theme", "Yaru")
    sandbox.command("gsettings", "set", "org.gnome.nautilus.preferences", "show-image-thumbnails", "never")
    sandbox.command(
        "gsettings", "set", "org.gnome.nautilus.preferences", "default-folder-viewer", "list-view"
    )
    sandbox.command("gsettings", "set", "org.gnome.nautilus.window-state", "initial-size", "(1180, 600)")
    for key in ("default-visible-columns", "default-column-order"):
        sandbox.command(
            "gsettings", "set", "org.gnome.nautilus.list-view", key, "['name', 'CloudMarks::status', 'size']"
        )
    Gtk.init_check()
    Gtk.Settings.get_default().set_property("gtk-icon-theme-name", "Yaru")
    env = os.environ | {
        "CM_M3_LOG": str(work / "status.json"),
        "CM_M3_NAME": json.loads((work / "scene.json").read_text())["name"],
    }
    with (work / "nautilus.stderr").open("w") as stderr:
        app = subprocess.Popen(
            ["nautilus", "--new-window", str(alias)], env=env, stdout=stderr, stderr=stderr
        )
        try:
            deadline = time.monotonic() + 10
            while True:
                try:
                    record = json.loads((work / "status.json").read_text())
                except (FileNotFoundError, ValueError):
                    record = {}
                if record.get("label", "").startswith("Kept offline (test account, read-only)"):
                    break
                if app.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("Real account status did not reach the read-only adapter")
                time.sleep(0.1)
            time.sleep(0.3)
            windows = subprocess.check_output(["xwininfo", "-root", "-tree"], text=True)
            for line in windows.splitlines():
                if '"CloudMarks read-only test"' in line:
                    match = re.search(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)", line)
                    if match:
                        width, height, x, y = map(int, match.groups())
                        ImageGrab.grab(
                            bbox=(x + 240, y, x + width, y + height), xdisplay=os.environ["DISPLAY"]
                        ).save(work / "account-readonly.png")
                        break
            else:
                raise RuntimeError("Cannot find the isolated account view")
            (work / "result.public.json").write_text(
                json.dumps(
                    {
                        "displayed_state": "kept_offline",
                        "displayed_scope": "test account, read-only",
                        "origin": "previously verified disposable OneDrive file",
                        "view": "no-network local-only mount mirrored read-only into isolated Files",
                        "adapter_actions": False,
                        "daily_desktop_changed": False,
                    },
                    indent=2,
                )
                + "\n"
            )
        finally:
            subprocess.run(["nautilus", "--quit"], capture_output=True, timeout=10)
            app.wait(timeout=10)
    install.uninstall()
    observer.unlink()


OBSERVER = r"""import gi,json,os
gi.require_version("Nautilus","4.1")
from gi.repository import GObject,GLib,Nautilus
class CloudMarksAccountObserver(GObject.GObject,Nautilus.InfoProvider):
    def update_file_info(self,file):
        if file.get_name()==os.environ["CM_M3_NAME"]:
            GLib.timeout_add(100,self.sample,file)
    def sample(self,file):
        label=file.get_string_attribute("cloudmarks_status") or ""
        with open(os.environ["CM_M3_LOG"],"w") as stream: json.dump({"label":label},stream)
        return False
"""


def main():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--config")
    parser.add_argument("--path", default="pinned-fixture.txt")
    args, remaining = parser.parse_known_args()
    sandbox.inside = inside
    sandbox.__file__ = __file__
    sys.argv = [sys.argv[0], *remaining]
    if "--inside" in remaining:
        sandbox.main()
        return
    if not args.config:
        raise SystemExit("Explicit --config is required")
    from cloudmarks.helper import OfflineStatusService, serve
    from cloudmarks.install import json_bytes, write_new
    from cloudmarks.offline import OfflineController
    from cloudmarks.offline_config import load_offline
    from cloudmarks.onedrive import OneDrive
    from cloudmarks.paths import relative_parts
    from cloudmarks.state import ReadStore, StateStore
    from cloudmarks.view import mount

    if len(relative_parts(args.path)) != 1:
        raise SystemExit("This desktop scene requires a root-level disposable file")
    work = Path(remaining[remaining.index("--work") + 1]).absolute()
    work.mkdir(mode=0o700)
    runtime = work / "host-runtime"
    runtime.mkdir(mode=0o700)
    alias = work / "CloudMarks read-only test"
    alias.mkdir(mode=0o700)
    config, settings = load_offline(args.config)
    backend = OneDrive(config, settings)
    with StateStore(config) as store:
        OfflineController(config, store, backend, settings).keep(args.path)
    actual = {
        "version": 2,
        "mode": "onedrive",
        "enabled": True,
        "visible_root": settings["visible_root"],
        "core_config": str(Path(args.config).absolute()),
        "socket": str(runtime / "status.sock"),
    }
    mirror = dict(actual, visible_root=str(alias))
    write_new(work / "mirror-settings.json", json_bytes(mirror))
    write_new(work / "scene.json", json_bytes({"name": args.path}))
    stop = threading.Event()
    ready_mount = threading.Event()
    ready_helper = threading.Event()
    errors = []

    def view_worker():
        try:
            mount(config, settings, offline=True, ready=ready_mount.set, stop=stop.is_set)
        except Exception as error:
            errors.append(error)
            ready_mount.set()

    def helper_worker():
        try:
            with ReadStore(config) as store:
                serve(
                    actual,
                    store,
                    stop=stop.is_set,
                    ready=ready_helper.set,
                    service=OfflineStatusService(actual, store, settings),
                )
        except Exception as error:
            errors.append(error)
            ready_helper.set()

    threads = [
        threading.Thread(target=view_worker, daemon=True),
        threading.Thread(target=helper_worker, daemon=True),
    ]
    original_run = subprocess.run

    def bind_view(argv, *a, **kw):
        if isinstance(argv, list) and argv and argv[0] == "bwrap":
            at = argv.index("dbus-run-session")
            argv = [*argv[:at], "--ro-bind", settings["visible_root"], str(alias), *argv[at:]]
        return original_run(argv, *a, **kw)

    try:
        threads[0].start()
        assert ready_mount.wait(20) and not errors
        threads[1].start()
        assert ready_helper.wait(5) and not errors
        subprocess.run = bind_view
        sandbox.main()
    finally:
        subprocess.run = original_run
        stop.set()
        for thread in threads:
            if thread.ident is not None:
                thread.join(20)
        if errors:
            raise errors[0]
        assert all(not t.is_alive() for t in threads), "Account test worker failed to stop"


if __name__ == "__main__":
    main()
