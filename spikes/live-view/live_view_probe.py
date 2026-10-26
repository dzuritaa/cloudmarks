"""Can verified copies be published or evicted while the read-only view is mounted?

Disposable experiment. A local directory stands in for the OneDrive origin, so
no account, network or existing mount is touched. The view uses the same rclone
flags as cloudmarks/view.py. Run it once per filesystem under test:

    python3 spikes/live-view/live_view_probe.py --work /path/on/ntfs3/new-dir
    python3 spikes/live-view/live_view_probe.py --work /tmp/new-dir

The work directory must not exist; it is removed afterwards unless --keep.
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

SIZE = 8 * 1024 * 1024
FLAGS = [
    "--read-only",
    "--vfs-cache-mode=off",
    "--buffer-size=0",
    "--dir-cache-time=1s",
    "--poll-interval=0",
    "--file-perms=0444",
    "--dir-perms=0555",
    "--log-level=ERROR",
]


def content(tag):
    block = hashlib.sha256(tag.encode()).digest() * (4096 // 32)
    return block * (SIZE // len(block))


def digest(data):
    return hashlib.sha256(data).hexdigest()[:16]


def mounted(path):
    with open("/proc/self/mountinfo") as stream:
        return any(line.split()[4] == str(path) for line in stream)


class View:
    def __init__(self, rclone, work, mode):
        self.rclone, self.work, self.mode = rclone, work, mode
        self.mountpoint = work / f"view-{mode}"
        self.mountpoint.mkdir()
        conf = work / f"{mode}.conf"
        store = work / "store/local"
        if mode == "online":
            conf.write_text(
                f"[origin]\ntype = alias\nremote = {work / 'origin'}\n\n"
                f'[view]\ntype = union\nupstreams = "{store}:ro" origin::ro\nsearch_policy = epall\n'
            )
        else:
            conf.write_text(f"[view]\ntype = alias\nremote = {store}\n")
        self.argv = [rclone, "mount", "view:", str(self.mountpoint), "--config", str(conf), *FLAGS]

    def __enter__(self):
        self.process = subprocess.Popen(self.argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        deadline = time.monotonic() + 15
        while not mounted(self.mountpoint):
            if self.process.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("mount failed: " + self.process.stderr.read().decode()[:500])
            time.sleep(0.05)
        return self

    def __exit__(self, *exc):
        result = subprocess.run(["fusermount3", "-u", str(self.mountpoint)], capture_output=True)
        self.unmounted_cleanly = result.returncode == 0
        if result.returncode:
            subprocess.run(["fusermount3", "-uz", str(self.mountpoint)], capture_output=True)
        self.process.wait(10)

    def path(self, name):
        return self.mountpoint / name


def wait_for(predicate, timeout=10.0):
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        try:
            if predicate():
                return round(time.monotonic() - started, 3)
        except OSError:
            pass
        time.sleep(0.02)
    return None


def publish(work, name, data):
    """Atomic publication, as the controller does: complete file, then rename."""
    staging = work / "store/staging" / name
    staging.write_bytes(data)
    with open(staging, "rb") as stream:
        os.fsync(stream.fileno())
    target = work / "store/local" / name
    if target.exists():
        raise FileExistsError(target)
    os.rename(staging, target)


def evict(work, name):
    """Quarantine like the controller (move out of the visible tree), not delete."""
    os.rename(work / "store/local" / name, work / "store/recovery" / name)


def read_view(view, name):
    with open(view.path(name), "rb") as stream:
        return stream.read()


def scenario_publish(view, work, name, expected):
    publish(work, name, expected)
    seconds = wait_for(lambda: read_view(view, name) == expected)
    return {"visible_after_seconds": seconds, "correct": seconds is not None}


def scenario_evict_closed(view, work, name, fallback):
    evict(work, name)
    if fallback is None:
        seconds = wait_for(lambda: not view.path(name).exists())
        return {"gone_after_seconds": seconds}
    seconds = wait_for(lambda: read_view(view, name) == fallback)
    return {"cloud_copy_after_seconds": seconds}


def scenario_evict_open(view, work, name, local, fallback):
    """A reader holds the local copy open while it is evicted."""
    result = {}
    with open(view.path(name), "rb") as stream:
        first = stream.read(SIZE // 2)
        evict(work, name)
        time.sleep(1.5)  # longer than the directory cache
        try:
            rest = stream.read()
            whole = first + rest
            if whole == local:
                result["open_reader"] = "kept reading the local copy"
            elif fallback is not None and whole == fallback:
                result["open_reader"] = "switched entirely to the cloud copy"
            else:
                result["open_reader"] = f"MIXED or truncated content ({len(whole)} bytes)"
        except OSError as error:
            result["open_reader"] = f"error {error.errno} ({os.strerror(error.errno)})"
    if fallback is None:
        result["after_close"] = (
            "gone" if wait_for(lambda: not view.path(name).exists()) is not None else "still visible"
        )
    else:
        ok = wait_for(lambda: read_view(view, name) == fallback) is not None
        result["after_close"] = "cloud copy" if ok else "stale local copy"
    return result


def scenario_publish_while_cloud_open(view, work, name, cloud, local):
    """Online only: a reader has the cloud copy open when a local copy is published."""
    with open(view.path(name), "rb") as stream:
        first = stream.read(SIZE // 2)
        publish(work, name, local)
        time.sleep(1.5)
        whole = first + stream.read()
    state = "kept reading the cloud copy" if whole == cloud else f"MIXED content ({digest(whole)})"
    ok = wait_for(lambda: read_view(view, name) == local) is not None
    return {"open_reader": state, "new_opens": "local copy" if ok else "still cloud copy"}


def scenario_folder(view, work):
    folder = work / "store/staging/folder"
    (folder / "nested").mkdir(parents=True)
    (folder / "nested/a.txt").write_bytes(b"a" * 1000)
    (folder / "empty").mkdir()
    os.rename(folder, work / "store/local/folder")
    seconds = wait_for(
        lambda: (
            view.path("folder/nested/a.txt").read_bytes() == b"a" * 1000
            and view.path("folder/empty").is_dir()
        )
    )
    return {"folder_visible_after_seconds": seconds}


def run(rclone, work):
    for name in ("store/local", "store/staging", "store/recovery", "origin"):
        (work / name).mkdir(parents=True)
    cloud, local = content("cloud revision"), content("retained local copy")
    filesystem = subprocess.run(
        ["findmnt", "-n", "-o", "FSTYPE", "-T", str(work)], capture_output=True, text=True
    ).stdout.split()[-1]
    results = {
        "filesystem": filesystem,
        "rclone": subprocess.run([rclone, "version"], capture_output=True, text=True).stdout.split()[1],
    }
    # Online view: union of local copies over the stand-in cloud.
    (work / "origin/shadow.bin").write_bytes(cloud)
    (work / "origin/evict-open.bin").write_bytes(cloud)
    (work / "origin/cloud-open.bin").write_bytes(cloud)
    with View(rclone, work, "online") as view:
        online = {}
        online["publish_new_file"] = scenario_publish(view, work, "new.bin", local)
        online["publish_over_cloud_copy"] = scenario_publish(view, work, "shadow.bin", local)
        online["evict_closed"] = scenario_evict_closed(view, work, "shadow.bin", cloud)
        publish(work, "evict-open.bin", local)
        wait_for(lambda: read_view(view, "evict-open.bin") == local)
        online["evict_while_open"] = scenario_evict_open(view, work, "evict-open.bin", local, cloud)
        online["publish_while_cloud_open"] = scenario_publish_while_cloud_open(
            view, work, "cloud-open.bin", cloud, local
        )
        online["publish_folder"] = scenario_folder(view, work)
    online["unmounted_cleanly"] = view.unmounted_cleanly
    results["online"] = online
    # Offline view: retained local copies only.
    for name in os.listdir(work / "store/local"):
        path = work / "store/local" / name
        shutil.rmtree(path) if path.is_dir() else path.unlink()
    with View(rclone, work, "offline") as view:
        offline = {}
        offline["publish_new_file"] = scenario_publish(view, work, "kept.bin", local)
        offline["evict_closed"] = scenario_evict_closed(view, work, "kept.bin", None)
        publish(work, "open.bin", local)
        wait_for(lambda: read_view(view, "open.bin") == local)
        offline["evict_while_open"] = scenario_evict_open(view, work, "open.bin", local, None)
        offline["publish_folder"] = scenario_folder(view, work)
    offline["unmounted_cleanly"] = view.unmounted_cleanly
    results["offline"] = offline
    return results


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--rclone", default=shutil.which("rclone"))
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()
    work = args.work.resolve()
    if work.exists():
        parser.error("--work must be a new directory")
    work.mkdir(parents=True)
    try:
        print(json.dumps(run(args.rclone, work), indent=2))
    finally:
        if not args.keep:
            for mountpoint in work.glob("view-*"):
                if mounted(mountpoint):
                    subprocess.run(["fusermount3", "-uz", str(mountpoint)], capture_output=True)
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
