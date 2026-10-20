#!/usr/bin/env python3
"""M0.3 opt-in isolated test-account experiment. Creates disposable cloud data.

Requires explicit private account settings and a blocked-drive record. Writes
only to a newly created, uniquely named remote folder. Never deletes cloud
content. It does not modify the existing mount or its configuration.
"""

import argparse
import configparser
import hashlib
import http.server
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
import uuid
from pathlib import Path

FIXTURE = "pinned-fixture.txt"
CONTENT = b"CloudMarks disposable, read-only offline fixture.\n" * 2048


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_only_checks(path):
    operations = {
        "write_existing": lambda: path.open("r+b"),
        "create_sibling": lambda: (path.parent / "must-not-create.txt").open("xb"),
        "rename_existing": lambda: path.rename(path.with_name("must-not-rename.txt")),
        "delete_existing": lambda: path.unlink(),
    }
    outcomes = {}
    for name, operation in operations.items():
        try:
            value = operation()
            if hasattr(value, "close"):
                value.close()
            outcomes[name] = False
        except OSError as error:
            outcomes[name] = error.errno == 30  # EROFS, rather than a coincidental permission failure
    return outcomes


def mount_check(work, rclone, *, offline, env=None):
    mount = work / "view"
    mount.mkdir(exist_ok=True)
    argv = [
        rclone,
        "mount",
        "view:",
        str(mount),
        "--config",
        str(work / "experiment.conf"),
        "--read-only",
        "--vfs-cache-mode=off",
        "--dir-cache-time=1s",
        "--poll-interval=0",
        "--cache-dir",
        str(work / "vfs-cache"),
        "--contimeout=1s",
        "--timeout=2s",
        "--retries=1",
        "--low-level-retries=1",
        "--log-level=ERROR",
    ]
    with (work / ("offline.stderr" if offline else "online.stderr")).open("w") as log:
        app = subprocess.Popen(argv, stdout=log, stderr=log, env=env)
        try:
            for _ in range(200):
                if mount.is_mount():
                    break
                if app.poll() is not None:
                    raise RuntimeError("Experimental mount exited before becoming ready")
                time.sleep(0.05)
            else:
                raise RuntimeError("Experimental mount timed out")
            # Child timeout prevents a blocked FUSE read from hanging the runner.
            reader = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import hashlib,pathlib,sys;print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())",
                    str(mount / FIXTURE),
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )
            if reader.returncode:
                raise RuntimeError("Experimental fixture read failed")
            readonly = read_only_checks(mount / FIXTURE)
            result = {"sha256": reader.stdout.strip(), "read_only": readonly}
            return result
        finally:
            if mount.is_mount():
                subprocess.run(["fusermount3", "-u", str(mount)], check=True, capture_output=True, timeout=10)
            if app.poll() is None:
                try:
                    app.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    app.terminate()
                    app.wait(timeout=10)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", type=Path)
    parser.add_argument("--blocked-drive-file", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--remote", default="cloudmarks-test")
    parser.add_argument("--work", required=True, type=Path)
    parser.add_argument("--rclone", default=shutil.which("rclone"))
    parser.add_argument(
        "--resume", action="store_true", help="Resume this experiment's private allowed-root record"
    )
    args = parser.parse_args()
    args.work = args.work.resolve()
    if not all((args.settings, args.blocked_drive_file, args.config, args.rclone)):
        parser.error("Explicit private settings, blocked-drive file, config, and rclone are required")
    if args.work.exists() and not args.resume:
        parser.error("Use a new work directory; an existing experiment must not be overwritten")
    args.work.mkdir(mode=0o700, parents=True, exist_ok=args.resume)
    os.chmod(args.work, 0o700)
    settings = json.loads(args.settings.read_text())
    blocked = json.loads(args.blocked_drive_file.read_text())["blocked_drive_id"].casefold()
    expected = settings["expected_drive_id"].casefold()
    config = configparser.ConfigParser(interpolation=None)
    config.read(args.config)
    remote = config[args.remote]
    if (
        not expected
        or expected == blocked
        or remote["drive_id"].casefold() != expected
        or remote["type"] != "onedrive"
    ):
        raise RuntimeError("Refusing mismatched or blocked configured drive")
    token = json.loads(remote["token"])["access_token"]

    def graph(route, method="GET", payload=None):
        if not route.startswith(("me/", "drives/")) or method not in ("GET", "POST", "PUT"):
            raise RuntimeError("Unexpected experiment API operation")
        data = (
            payload
            if isinstance(payload, bytes)
            else json.dumps(payload).encode()
            if payload is not None
            else None
        )
        request = urllib.request.Request(
            "https://graph.microsoft.com/v1.0/" + route,
            data=data,
            method=method,
            headers={
                "Authorization": "Bearer " + token,
                "Content-Type": "application/octet-stream"
                if isinstance(payload, bytes)
                else "application/json",
            },
        )
        return json.load(urllib.request.urlopen(request, timeout=30))

    def identity():
        drive = graph("me/drive?$select=id,driveType")
        me = graph("me/?$select=mail,userPrincipalName")
        aliases = {str(me.get(key, "")).casefold() for key in ("mail", "userPrincipalName")}
        if (
            drive["id"].casefold() != expected
            or drive["id"].casefold() == blocked
            or settings["expected_email"].casefold() not in aliases
        ):
            raise RuntimeError("Refusing mismatched authenticated account or drive")
        return drive

    drive = identity()
    if args.resume:
        context = json.loads((args.work / "private-context.json").read_text())
        if context["expected_drive_id"].casefold() != expected:
            raise RuntimeError("Cannot resume an experiment for a different drive")
        root_name = context["allowed_root_name"]
        folder = graph("me/drive/items/" + context["allowed_root_id"] + "?$select=id,name,parentReference")
    else:
        root_name = "CloudMarks-M0-" + uuid.uuid4().hex
        folder = graph(
            "me/drive/root/children",
            "POST",
            {"name": root_name, "folder": {}, "@microsoft.graph.conflictBehavior": "fail"},
        )
    if folder["name"] != root_name or folder["parentReference"]["driveId"].casefold() != expected:
        raise RuntimeError("New folder does not match allowed test root")
    root_id = folder["id"]
    # Keep the configured allowed root and all machine/account identifiers private.
    private = {"allowed_root_id": root_id, "allowed_root_name": root_name, "expected_drive_id": drive["id"]}
    (args.work / "private-context.json").write_text(json.dumps(private, indent=2) + "\n")
    store = args.work / "store"
    store.mkdir(mode=0o700, exist_ok=args.resume)
    source = args.work / "source.txt"
    source.write_bytes(CONTENT)
    experiment = configparser.ConfigParser(interpolation=None)
    experiment["origin"] = dict(remote)
    # rclone's native OneDrive identity is driveID#itemID. A raw Graph item ID
    # alone is NOT a safe substitute: path operations can fall back to drive root.
    experiment["origin"]["root_folder_id"] = drive["id"] + "#" + root_id
    experiment["view"] = {"type": "union", "upstreams": f'"{store}:ro" origin::ro', "search_policy": "epall"}
    conf = args.work / "experiment.conf"
    with conf.open("w") as stream:
        experiment.write(stream)
    os.chmod(conf, 0o600)

    def rclone(*arguments):
        # This experiment supports only a file copy or read. Destination/source
        # remotes are constructed here, never accepted from external input.
        if arguments[0] not in ("copyto", "cat") or not arguments[1].startswith("origin:"):
            raise RuntimeError("Operation is outside the experiment allowlist")
        if arguments[0] == "copyto" and Path(arguments[2]).parent != store:
            raise RuntimeError("Downloads must target the experiment's local store")
        fresh = graph("me/drive/items/" + root_id + "?$select=id,name,parentReference")
        if (
            fresh["id"] != root_id
            or fresh["name"] != root_name
            or fresh["parentReference"]["driveId"].casefold() != expected
        ):
            raise RuntimeError("Allowed test root changed")
        identity()
        env = {key: value for key, value in os.environ.items() if not key.startswith("RCLONE_")}
        call = subprocess.run(
            [
                args.rclone,
                *arguments,
                "--config",
                str(conf),
                "--retries=1",
                "--low-level-retries=1",
                "--contimeout=5s",
                "--timeout=15s",
            ],
            env=env,
            capture_output=True,
            timeout=60,
        )
        if call.returncode:
            (args.work / "command-error.private").write_bytes(call.stderr)
            raise RuntimeError("Scoped rclone fixture operation failed; private diagnostic saved")
        return call.stdout

    def fixture_info():
        items = graph("me/drive/items/" + root_id + "/children?$select=id,name,eTag,size,parentReference")[
            "value"
        ]
        matches = [item for item in items if item["name"] == FIXTURE]
        if len(matches) != 1 or matches[0]["parentReference"]["id"].casefold() != root_id.casefold():
            raise RuntimeError("Fixture is not uniquely contained by the allowed root")
        return matches[0]

    if not args.resume:
        # Provision by Graph folder ID; rclone is read-only in this experiment.
        graph("me/drive/items/" + root_id + ":/" + FIXTURE + ":/content", "PUT", CONTENT)
    before = fixture_info()
    temporary = store / ".partial"
    rclone("copyto", "origin:" + FIXTURE, str(temporary))
    expected_hash = hashlib.sha256(CONTENT).hexdigest()
    if digest(temporary) != expected_hash or temporary.stat().st_size != len(CONTENT):
        raise RuntimeError("Incomplete or invalid fixture download")
    temporary.replace(store / FIXTURE)
    online = mount_check(args.work, args.rclone, offline=False)
    offline_results = []

    class DenyProxy(http.server.BaseHTTPRequestHandler):
        requests_rejected = 0

        def do_CONNECT(self):
            type(self).requests_rejected += 1
            self.send_error(503, "CloudMarks controlled offline test")

        do_GET = do_CONNECT

        def log_message(self, *a):
            pass

    # AppArmor blocks fusermount3 under Bubblewrap on this host. Do not change
    # that policy. Deny HTTP(S) for these processes only, and first prove that
    # a direct remote read cannot bypass it. No global network changes.
    proxy = http.server.ThreadingHTTPServer(("127.0.0.1", 0), DenyProxy)
    proxy.daemon_threads = True
    proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    proxy_thread.start()
    proxy_url = f"http://127.0.0.1:{proxy.server_port}"
    offline_env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("RCLONE_")
        and key.lower() not in ("http_proxy", "https_proxy", "all_proxy", "no_proxy")
    }
    offline_env.update({"HTTP_PROXY": proxy_url, "HTTPS_PROXY": proxy_url, "NO_PROXY": ""})
    try:
        canary = subprocess.run(
            [
                args.rclone,
                "cat",
                "origin:" + FIXTURE,
                "--config",
                str(conf),
                "--retries=1",
                "--low-level-retries=1",
                "--contimeout=1s",
                "--timeout=2s",
            ],
            env=offline_env,
            capture_output=True,
            timeout=15,
        )
        if canary.returncode == 0 or DenyProxy.requests_rejected == 0:
            raise RuntimeError("Offline simulation did not block a direct remote read")
        for _ in range(2):
            offline_results.append(mount_check(args.work, args.rclone, offline=True, env=offline_env))
    finally:
        proxy.shutdown()
        proxy.server_close()
    assert not (args.work / "view").is_mount(), "Refusing to evict while the experimental view is mounted"
    assert digest(store / FIXTURE) == expected_hash
    (store / FIXTURE).unlink()  # An explicit local-store path, never a mounted/cloud path.
    after = fixture_info()
    remote_hash = hashlib.sha256(rclone("cat", "origin:" + FIXTURE)).hexdigest()
    result = {
        "drive_identity_verified": True,
        "test_account_verified": True,
        "root_enforced": True,
        "expected_sha256": expected_hash,
        "bytes": len(CONTENT),
        "online": online,
        "offline_restarts": offline_results,
        "offline_simulation": {
            "method": "per-process rejecting HTTP(S) proxy; host remains online",
            "direct_remote_read_blocked": True,
            "rejected_requests": DenyProxy.requests_rejected,
            "network_namespace_fuse_test": "unavailable: AppArmor prevents sandboxed fusermount3",
        },
        "local_eviction": {
            "local_removed": not (store / FIXTURE).exists(),
            "remote_id_unchanged": before["id"] == after["id"],
            "remote_revision_unchanged": before["eTag"] == after["eTag"],
            "remote_sha256": remote_hash,
        },
        "cloud_fixture_retained": True,
    }
    assert online["sha256"] == expected_hash and all(online["read_only"].values())
    assert all(r["sha256"] == expected_hash and all(r["read_only"].values()) for r in offline_results)
    assert remote_hash == expected_hash and result["local_eviction"]["remote_revision_unchanged"]
    (args.work / "result.public.json").write_text(json.dumps(result, indent=2) + "\n")
    (args.work / "exception.private").unlink(missing_ok=True)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Do not expose Graph URLs, account IDs, or rclone output in public logs.
        if "--work" in sys.argv:
            directory = Path(sys.argv[sys.argv.index("--work") + 1])
            if directory.is_dir():
                diagnostic = directory / "exception.private"
                diagnostic.write_text(traceback.format_exc())
                diagnostic.chmod(0o600)
        print(
            f"Experiment stopped: {type(error).__name__}. Inspect its private work directory.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
