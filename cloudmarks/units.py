"""The three systemd user units that setup generates, and safe quoting for them.

Pure text generation: nothing here writes files or talks to systemd.
"""

import re

from .model import ConfigurationError

UPLOAD_UNIT = "cloudmarks-upload.service"
STATUS_UNIT = "cloudmarks-status.service"
VIEW_UNIT = "cloudmarks-view.service"


def unit_word(value):
    """Quote one systemd command-line word; refuse control characters."""
    value = str(value)
    if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ConfigurationError("Unit paths cannot contain control characters")
    if re.fullmatch(r"[A-Za-z0-9_./:=@+,-]+", value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
    return f'"{escaped}"'


def unit_path(value):
    value = str(value)
    if any(ord(char) < 32 or ord(char) == 127 for char in value) or re.search(r"\s", value):
        raise ConfigurationError("Unit setting paths cannot contain whitespace or control characters")
    return value.replace("%", "%%")


def command(*words):
    return " ".join(unit_word(word) for word in words)


def render_units(paths, *, python, rclone):
    """Return the three user units, modelled on the validated trial services."""
    desktop = paths["desktop"]
    guard = (python, "-m", "cloudmarks", "mount-guard", "--settings", desktop)
    upload = f"""[Unit]
Description=CloudMarks upload folder (writable rclone mount)
Documentation=https://github.com/dzuritaa/cloudmarks

[Service]
Type=notify
ExecStartPre={command(*guard, "--view", "upload", "--operation", "prepare")}
ExecStart={
        command(
            rclone,
            "mount",
            "upload:",
            paths["upload_root"],
            "--config=" + paths["upload_config"],
            "--cache-dir=" + paths["upload_cache"],
            "--vfs-cache-mode=writes",
            "--vfs-cache-max-size=1G",
            f"--vfs-cache-min-free-space={paths['reserve_gib']}G",
            "--vfs-write-back=2s",
            "--dir-cache-time=2s",
            "--poll-interval=0",
            "--buffer-size=0",
            "--contimeout=5s",
            "--timeout=15s",
            "--umask=077",
            "--devname=cloudmarks-upload",
            "--log-level=ERROR",
            "--rc",
            "--rc-addr=unix://" + paths["upload_socket"],
        )
    }
ExecStop={command("fusermount3", "-u", paths["upload_root"])}
ExecStopPost={command(*guard, "--view", "upload", "--operation", "protect")}
UMask=0077
RuntimeDirectory=cloudmarks-upload
RuntimeDirectoryMode=0700
Restart=on-failure
RestartSec=10
SyslogIdentifier=cloudmarks-upload
KillMode=mixed
KillSignal=SIGINT
SendSIGKILL=no
TimeoutStartSec=45
TimeoutStopSec=30

[Install]
WantedBy=default.target
"""
    status = f"""[Unit]
Description=CloudMarks status helper
Documentation=https://github.com/dzuritaa/cloudmarks
Wants={UPLOAD_UNIT}
After={UPLOAD_UNIT}

[Service]
Type=simple
ExecStart={command(python, paths["helper"], "serve", "--settings", desktop)}
UMask=0077
RuntimeDirectory=cloudmarks
RuntimeDirectoryMode=0700
Restart=on-failure
RestartSec=3
TimeoutStopSec=5
NoNewPrivileges=true
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6

[Install]
WantedBy=default.target
"""
    view = f"""[Unit]
Description=CloudMarks offline view (read-only)
Documentation=https://github.com/dzuritaa/cloudmarks
Wants={STATUS_UNIT}
After={STATUS_UNIT}

[Service]
Type=simple
EnvironmentFile=-{unit_path(paths["mount_env"])}
ExecStartPre={command(*guard, "--view", "offline", "--operation", "prepare")}
ExecStart={command(python, "-m", "cloudmarks", "offline", "--config", paths["offline"], "mount")} $CLOUDMARKS_VIEW_FLAGS
ExecStopPost={command(*guard, "--view", "offline", "--operation", "protect")}
UMask=0077
Restart=on-failure
RestartSec=10
KillMode=mixed
KillSignal=SIGINT
SendSIGKILL=no
TimeoutStopSec=30

[Install]
WantedBy=default.target
"""
    return {UPLOAD_UNIT: upload, STATUS_UNIT: status, VIEW_UNIT: view}
