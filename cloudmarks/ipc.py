"""Bounded, versioned, same-user local status protocol. No mutation operations."""

import json
import os
import re
import socket
import stat
import struct
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .model import CloudMarksError, ConfigurationError
from .paths import open_absolute_directory, relative_parts
from .telemetry import error_category, record

VERSION = 1
MAX_FRAME = 16384
MAX_BATCH = 16
TIMEOUT = 0.6
LEASE_MS = 1000
STATES = {
    "cloud_only",
    "saved_locally",
    "kept_offline",
    "transferring",
    "uploading",
    "partial",
    "freeing",
    "error",
    "unknown",
}
UNKNOWN = {"state": "unknown", "reason": "Helper unavailable or evidence needs checking", "valid_for_ms": 500}


def private_directory(path, *, create=False):
    path = Path(path)
    if create:
        path.mkdir(mode=0o700, exist_ok=True)
    fd = open_absolute_directory(path)
    info = os.fstat(fd)
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        os.close(fd)
        raise ConfigurationError("IPC/configuration directory must be user-owned and private (0700)")
    return fd


def read_private_json(path):
    path = Path(path)
    parent = private_directory(path.parent)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077
            ):
                raise ConfigurationError("Settings must be a private regular file owned by this user")
            data = stream.read(MAX_FRAME + 1)
        if len(data) > MAX_FRAME:
            raise ConfigurationError("Settings exceed size limit")
        return json.loads(data)
    finally:
        os.close(parent)


def validate_settings(value):
    base = {"version", "mode", "enabled", "visible_root", "core_config", "socket"}
    automatic = isinstance(value, dict) and value.get("version") in (3, 4)
    controls = isinstance(value, dict) and value.get("version") == 4
    if (
        not isinstance(value, dict)
        or set(value)
        != base
        | ({"upload_root", "upload_config", "upload_socket"} if automatic else set())
        | ({"control_config", "control_binding"} if controls else set())
        or type(value["version"]) is not int
        or (value["version"], value["mode"])
        not in ((1, "fixture"), (2, "onedrive"), (3, "onedrive"), (4, "onedrive"))
        or type(value["enabled"]) is not bool
    ):
        raise ConfigurationError("Expected version 1 fixture or version 2 test-account desktop settings")
    for key in (
        ("visible_root", "core_config", "socket")
        + (("upload_root", "upload_config", "upload_socket") if automatic else ())
        + (("control_config",) if controls else ())
    ):
        item = value[key]
        if (
            not isinstance(item, str)
            or not item.startswith("/")
            or "\x00" in item
            or ".." in Path(item).parts
            or str(Path(item)) != item
            or item == "/"
        ):
            raise ConfigurationError("Desktop paths must be canonical absolute non-root paths")
    if len(os.fsencode(value["socket"])) > 100:
        raise ConfigurationError("Local socket pathname is too long")
    if automatic:
        a, b = Path(value["visible_root"]), Path(value["upload_root"])
        if a.is_relative_to(b) or b.is_relative_to(a) or len(os.fsencode(value["upload_socket"])) > 100:
            raise ConfigurationError("Upload and offline roots must be separate; sockets must be bounded")
    if controls and (
        not isinstance(value["control_binding"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", value["control_binding"])
    ):
        raise ConfigurationError("Control binding must be a SHA-256 configuration fingerprint")
    return value


def load_settings(path):
    return validate_settings(read_private_json(path))


def relative_uri(uri, root):
    """Lexical mapping only; helper independently validates scope and open paths."""
    if not isinstance(uri, str) or len(uri) > 16384:
        return None
    try:
        parsed = urlsplit(uri)
        if parsed.scheme != "file" or parsed.netloc or parsed.query or parsed.fragment:
            return None
        path = unquote(parsed.path, errors="strict")
        prefix = root + "/"
        if not path.startswith(prefix):
            return None
        relative = path[len(prefix) :]
        relative_parts(relative)
        return relative
    except (ValueError, UnicodeError, CloudMarksError):
        return None


def mapped_uri(uri, settings):
    """Return a presentation view and relative path, without touching either mount."""
    if settings:
        for view, root in (("offline", settings["visible_root"]), ("upload", settings.get("upload_root"))):
            if root and (path := relative_uri(uri, root)) is not None:
                return view, path
    return None


def encode(value):
    data = json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode() + b"\n"
    if len(data) > MAX_FRAME:
        raise ValueError("Protocol frame too large")
    return data


def receive(connection):
    deadline = time.monotonic() + TIMEOUT
    data = bytearray()
    while len(data) <= MAX_FRAME:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Protocol deadline exceeded")
        connection.settimeout(remaining)
        chunk = connection.recv(min(4096, MAX_FRAME + 1 - len(data)))
        if not chunk:
            raise ValueError("Incomplete protocol frame")
        data.extend(chunk)
        if b"\n" in chunk:
            if not data.endswith(b"\n") or data.count(b"\n") != 1 or len(data) > MAX_FRAME:
                raise ValueError("Expected one bounded frame")
            return json.loads(data)
    raise ValueError("Protocol frame too large")


def same_user(connection):
    _, uid, _ = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    return uid == os.getuid()


def request(socket_path, paths, *, view="offline"):
    """Blocking client for background workers only. Fail closed on every error."""
    fallback = {path: dict(UNKNOWN) for path in paths}

    def failed(category):
        record("ipc", "ipc_failed", error=category, count=len(paths))
        return fallback

    try:
        if (
            view not in ("offline", "upload")
            or not 1 <= len(paths) <= MAX_BATCH
            or len(set(paths)) != len(paths)
        ):
            return failed("protocol")
        for path in paths:
            relative_parts(path)
        parent = private_directory(Path(socket_path).parent)
        try:
            info = os.stat(Path(socket_path).name, dir_fd=parent, follow_symlinks=False)
            if (
                not stat.S_ISSOCK(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077
            ):
                return failed("permissions")
        finally:
            os.close(parent)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(TIMEOUT)
            connection.connect(str(socket_path))
            if not same_user(connection):
                return failed("permissions")
            frame = {"version": VERSION, "op": "status", "paths": paths}
            if view != "offline":
                frame["view"] = view
            connection.sendall(encode(frame))
            response = receive(connection)
        if (
            not isinstance(response, dict)
            or set(response) != {"version", "results"}
            or type(response["version"]) is not int
            or response["version"] != VERSION
            or not isinstance(response["results"], dict)
            or set(response["results"]) != set(paths)
        ):
            return failed("protocol")
        for result in response["results"].values():
            if (
                not isinstance(result, dict)
                or set(result) != {"state", "reason", "valid_for_ms"}
                or result["state"] not in STATES
                or not isinstance(result["reason"], str)
                or len(result["reason"]) > 256
                or any(ord(c) < 32 for c in result["reason"])
                or type(result["valid_for_ms"]) is not int
                or not 1 <= result["valid_for_ms"] <= LEASE_MS
            ):
                return failed("protocol")
        return response["results"]
    except (OSError, ValueError, TypeError, KeyError, CloudMarksError) as error:
        return failed(error_category(error))
