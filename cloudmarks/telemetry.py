"""Allowlisted, nonblocking diagnostics to the local systemd journal.

Never pass exception text, command lines, paths, identifiers or response bodies.
The journal supplies timestamps/PIDs and manages storage and rotation. Failure
to log must never change a file operation or status result.
"""

import json
import math
import os
import socket
import threading
import time

from . import __version__

IDENTIFIER = "cloudmarks-diagnostics"
COMPONENTS = {"cli", "desktop", "bridge", "helper", "ipc", "view", "transport"}
EVENTS = {
    "command_started",
    "command_finished",
    "command_failed",
    "settings_changed",
    "worker_started",
    "worker_stopped",
    "worker_failed",
    "status_summary",
    "response_slow",
    "response_late",
    "lease_expired",
    "pending_timeout",
    "status_changed",
    "helper_started",
    "helper_stopped",
    "request_rejected",
    "request_completed",
    "budget_exhausted",
    "ipc_failed",
    "mount_starting",
    "mount_ready",
    "mount_stopped",
    "mount_failed",
    "unmount_failed",
    "metadata_failed",
    "download_failed",
}
STATES = {"none", "cloud_only", "saved_locally", "kept_offline", "transferring", "error", "unknown"}
ENUMS = {
    "state": STATES,
    "previous_state": STATES,
    "mode": {"fixture", "onedrive", "online", "offline"},
    "outcome": {"ok", "failed", "interrupted", "eof", "stopped"},
    "error": {
        "timeout",
        "socket",
        "permissions",
        "protocol",
        "configuration",
        "scope",
        "integrity",
        "storage",
        "unexpected",
    },
    "command": {
        "demo",
        "check-config",
        "serve",
        "fixture-session",
        "install",
        "enable",
        "disable",
        "uninstall",
        "offline",
        "logs",
    },
    "action": {
        "init",
        "recover",
        "jobs",
        "conflicts",
        "snapshots",
        "changes",
        "resume",
        "archives",
        "doctor",
        "keep",
        "retry",
        "unpin",
        "free",
        "status",
        "queue",
        "refresh",
        "queue-tree",
        "unpin-tree",
        "verify",
        "resolve",
        "export-archive",
        "mount",
    },
}
COUNTS = {"count", "unknown_count", "positive_count", "error_count", "exit_code", "status_code"}
TIMINGS = {"elapsed_ms", "remaining_ms", "late_ms"}
BOOLS = {"enabled", "available"}
_lock = threading.Lock()
_windows = {}
_clock = time.monotonic


def enabled(level="info"):
    configured = os.environ.get("CLOUDMARKS_LOG_LEVEL", "info").lower()
    return configured != "off" and (level != "debug" or configured == "debug")


def error_category(error):
    from .model import ConfigurationError, IntegrityError, ScopeError, StorageError

    for kind, category in (
        (TimeoutError, "timeout"),
        (PermissionError, "permissions"),
        (ConfigurationError, "configuration"),
        (ScopeError, "scope"),
        (IntegrityError, "integrity"),
        (StorageError, "storage"),
        (OSError, "socket"),
        (ValueError, "protocol"),
    ):
        if isinstance(error, kind):
            return category
    return "unexpected"


def message(component, event, fields):
    """Pure formatting: reject all free text and unknown fields at the boundary."""
    if component not in COMPONENTS or event not in EVENTS:
        return None
    result = {"version": __version__, "component": component, "event": event}
    for key, value in fields.items():
        if key in ENUMS and isinstance(value, str) and value in ENUMS[key]:
            result[key] = value
        elif key in COUNTS and type(value) is int and (-255 if key == "exit_code" else 0) <= value <= 1000000:
            result[key] = value
        elif key in TIMINGS and (type(value) is int or type(value) is float and math.isfinite(value)):
            result[key] = round(max(0, min(86400000, value)), 3)
        elif key in BOOLS and type(value) is bool:
            result[key] = value
    return result


def record(component, event, *, level="info", **fields):
    if not enabled(level):
        return False
    if level not in {"info", "debug", "warning"}:
        return False
    payload = message(component, event, fields)
    if payload is None or not _lock.acquire(blocking=False):
        return False
    try:
        # Also bound repeated failures/debug requests when the desktop is busy.
        now = _clock()
        key = (component, event)
        started, count = _windows.get(key, (now, 0))
        if now - started >= 10:
            started, count = now, 0
        if count >= 10:
            return False
        _windows[key] = (started, count + 1)
        data = (
            f"SYSLOG_IDENTIFIER={IDENTIFIER}\n"
            f"PRIORITY={7 if level == 'debug' else 4 if level == 'warning' else 6}\n"
            "MESSAGE=" + json.dumps(payload, separators=(",", ":")) + "\n"
        ).encode()
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_NONBLOCK) as stream:
            stream.sendto(data, "/run/systemd/journal/socket")
        return True
    except (OSError, ValueError, TypeError):
        return False
    finally:
        _lock.release()
