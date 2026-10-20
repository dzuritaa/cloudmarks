#!/usr/bin/env python3
"""M0.3 fixture-only rclone VFS experiment against a disposable HTTP origin.

This never reads credentials or contacts OneDrive. It shuts down only its own
HTTP server to reproduce unavailable-origin behavior. No global networking is
changed and no running user's cache is touched.
"""

import argparse
import hashlib
import http.server
import json
import shutil
import subprocess
import threading
import time
from pathlib import Path


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def read(path):
    result = subprocess.run(
        [
            "/usr/bin/python3",
            "-c",
            "import hashlib,pathlib,sys; print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=12,
    )
    return {"ok": result.returncode == 0, "sha256": result.stdout.strip() if result.returncode == 0 else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", required=True, type=Path)
    parser.add_argument("--rclone", default=shutil.which("rclone"))
    parser.add_argument("--layout", choices=("vfs", "union"), default="vfs")
    args = parser.parse_args()
    args.work = args.work.resolve()
    args.work.mkdir(parents=True, exist_ok=True)
    origin = args.work / "origin"
    mount = args.work / "mount"
    origin.mkdir(exist_ok=True)
    mount.mkdir(exist_ok=True)
    content = b"CloudMarks disposable offline-read fixture.\n" * 1024
    (origin / "complete.txt").write_bytes(content)
    (origin / "remote-only.txt").write_text("Disposable file deliberately absent from the offline store.\n")

    def handler(*a, **kw):
        return QuietHandler(*a, directory=str(origin), **kw)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/"
    argv = [
        args.rclone,
        "mount",
        ":http:",
        str(mount),
        "--http-url",
        url,
        "--read-only",
        "--vfs-cache-mode=full",
        "--cache-dir",
        str(args.work / "cache"),
        "--vfs-cache-max-age=168h",
        "--dir-cache-time=168h",
        "--contimeout=1s",
        "--timeout=2s",
        "--retries=1",
        "--low-level-retries=1",
        "--log-level=ERROR",
        "--config",
        "/dev/null",
    ]
    if args.layout == "union":
        store = args.work / "store"
        store.mkdir(exist_ok=True)
        (store / "complete.txt").write_bytes(content)
        config = args.work / "fixture.conf"
        config.write_text(
            f'[origin]\ntype = http\nurl = {url}\n\n[view]\ntype = union\nupstreams = "{store}:ro" origin::ro\nsearch_policy = epall\n'
        )
        argv = [
            args.rclone,
            "mount",
            "view:",
            str(mount),
            "--read-only",
            "--vfs-cache-mode=off",
            "--contimeout=1s",
            "--timeout=2s",
            "--retries=1",
            "--low-level-retries=1",
            "--config",
            str(config),
            "--log-level=ERROR",
        ]
    process = None
    log = (args.work / "rclone.stderr").open("w")

    def start():
        app = subprocess.Popen(argv, stdout=log, stderr=log)
        for _ in range(100):
            if mount.is_mount():
                return app
            if app.poll() is not None:
                raise RuntimeError("Fixture mount failed; inspect private rclone.stderr")
            time.sleep(0.05)
        app.terminate()
        app.wait(timeout=10)
        raise RuntimeError("Fixture mount did not become ready")

    def stop(app):
        subprocess.run(["fusermount3", "-u", str(mount)], check=True, capture_output=True, timeout=10)
        app.wait(timeout=10)

    try:
        process = start()
        online = read(mount / "complete.txt")
        server.shutdown()
        server.server_close()
        offline_warm = read(mount / "complete.txt")
        stop(process)
        process = None
        process = start()
        offline_restart = read(mount / "complete.txt")
        listing = subprocess.run(
            [
                "/usr/bin/python3",
                "-c",
                "import json,pathlib,sys;print(json.dumps(sorted(p.name for p in pathlib.Path(sys.argv[1]).iterdir())))",
                str(mount),
            ],
            capture_output=True,
            text=True,
            timeout=12,
        )
        offline_entries = json.loads(listing.stdout) if listing.returncode == 0 else None
        result = {
            "rclone": subprocess.check_output([args.rclone, "version"], text=True).splitlines()[0],
            "expected_sha256": hashlib.sha256(content).hexdigest(),
            "online": online,
            "offline_same_process": offline_warm,
            "offline_after_restart": offline_restart,
            "origin_unavailable_method": "fixture HTTP server stopped; no system network changes",
            "layout": args.layout,
            "offline_restart_listing": {"ok": listing.returncode == 0, "entries": offline_entries},
        }
        (args.work / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result, indent=2))
    finally:
        if process is not None:
            if mount.is_mount():
                stop(process)
            elif process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
        server.shutdown()
        server.server_close()
        log.close()


if __name__ == "__main__":
    main()
