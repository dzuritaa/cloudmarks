"""Explicit artificial desktop session. Never opens or discovers credentials."""

import json
import os
import time
from dataclasses import replace
from pathlib import Path

from .config import Config
from .fixture import FixtureBackend, FixtureController
from .install import json_bytes, write_new
from .model import FileIdentity
from .paths import MARKER
from .state import StateStore


def create(directory, *, count=0):
    directory = Path(directory)
    directory.mkdir(mode=0o700)  # Must be new: never overwrite user content.
    disk, state, view, runtime = (
        directory / name for name in ("disk", "state", "CloudMarks fixtures", "run")
    )
    for path in (disk, state, view, runtime):
        path.mkdir(mode=0o700)
    for path in (disk, view):
        (path / MARKER).write_text("fixture-volume\n")
    config = Config(
        "fixture-test-drive",
        "fixture-root",
        ("fixture-personal-drive",),
        disk,
        "fixture-volume",
        state / "state.sqlite3",
        max_age_seconds=3600,
        case_sensitive=True,
    )
    core = directory / "core.toml"
    # JSON quoted strings are a valid TOML subset for these generated paths.
    core.write_text(f"""version = 1
[account]
expected_drive_id = "fixture-test-drive"
allowed_root_id = "fixture-root"
blocked_drive_ids = ["fixture-personal-drive"]
[storage]
root = {json.dumps(str(disk))}
volume_id = "fixture-volume"
state_path = {json.dumps(str(config.state_path))}
case_sensitive = true
[status]
max_age_seconds = 3600
""")
    os.chmod(core, 0o600)
    settings = {
        "version": 1,
        "mode": "fixture",
        "enabled": True,
        "visible_root": str(view),
        "core_config": str(core),
        "socket": str(runtime / "status.sock"),
    }
    write_new(directory / "desktop.json", json_bytes(settings))
    store = StateStore(config)
    try:
        backend = FixtureBackend(config)
        controller = FixtureController(config, store, backend)
        for index, name in enumerate(
            (
                "01-cloud.txt",
                "02-local.txt",
                "03-pinned.txt",
                "04-transfer.txt",
                "05-error.txt",
                "06-unknown.txt",
                "07-refresh.txt",
            )
        ):
            identity = FileIdentity(config.expected_drive_id, name)
            content = f"Artificial CloudMarks fixture {name}.\n".encode()
            remote = backend.put(identity, name, content)
            if index == 5:
                remote = replace(remote, observed_at=time.time() - 7200)
            store.observe(remote)
            (view / name).write_bytes(content)
            os.chmod(view / name, 0o444)
            if index in (1, 2, 6):
                controller.download(identity)
            if index == 2:
                store.set_pin(identity, True)
            if index == 3:
                store.begin_transfer(identity)
            if index == 4:
                store.fail_transfer(identity, "Artificial failed download; no cloud request was made")
        folder = view / "Large folder"
        folder.mkdir()
        for index in range(count):
            name = f"Large folder/item-{index:05}.txt"
            remote = backend.put(FileIdentity(config.expected_drive_id, name), name, b"Artificial listing.\n")
            store.observe(remote)
            (view / name).write_bytes(b"Artificial listing.\n")
        (view / "08-no-observation.txt").write_text("No remote observation; must show unknown.\n")
        return settings, store
    except BaseException:
        store.close()
        raise
