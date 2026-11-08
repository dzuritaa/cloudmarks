"""Read-only health report with no account names, paths, IDs or credentials."""

import os
from typing import TypedDict

from .offline_config import mounted_view
from .paths import SecureRoot
from .state import StateStore
from .view import attest_view


class HealthReport(TypedDict, total=False):
    schema: int
    storage: str
    view: str
    counts: dict[str, int]
    free_bytes: int
    reserve_satisfied: bool
    attention_files: int
    unfinished_jobs: int
    state_problems: list[str]
    ready_for_trial: bool


def doctor(control) -> HealthReport:
    schema = control.store.connection.execute("PRAGMA user_version").fetchone()[0]
    report: HealthReport = {"schema": schema, "storage": "unavailable", "view": "unmounted", "counts": {}}
    try:
        control.volume_check(control.config, control.settings)
        with SecureRoot(control.config.storage_root, control.config.volume_id) as root:
            space = os.fstatvfs(root.fd)
            report["free_bytes"] = space.f_bavail * space.f_frsize
            report["reserve_satisfied"] = report["free_bytes"] >= control.settings["min_free_bytes"]
            root.revalidate(control.config.volume_id)
        report["storage"] = "verified"
    except Exception:
        pass
    try:
        if mounted_view(control.settings):
            report["view"] = (
                "verified_read_only" if attest_view(control.config, control.settings) else "unverified"
            )
    except Exception:
        report["view"] = "unavailable"
    for table in ("files", "copies", "publications", "jobs", "issues", "archives"):
        if (
            table == "publications"
            and schema < 2
            or table in ("jobs", "issues")
            and schema < 3
            or table == "archives"
            and schema < 4
        ):
            continue
        report["counts"][table] = control.store.connection.execute(
            f"SELECT count(*) FROM {table}"
        ).fetchone()[0]
    report["attention_files"] = control.store.connection.execute(
        "SELECT count(*) FROM files WHERE dirty=1 OR operation IN ('active','error')"
    ).fetchone()[0]
    if schema >= 3:
        unfinished_states = sorted(StateStore.JOB_STATES - {"done", "cancelled"})
        placeholders = ",".join("?" for _ in unfinished_states)
        report["unfinished_jobs"] = control.store.connection.execute(
            f"SELECT count(*) FROM jobs WHERE status IN ({placeholders})", unfinished_states
        ).fetchone()[0]
    report["state_problems"] = control.store.check_integrity()
    report["ready_for_trial"] = (
        state_healthy(report)
        and report["storage"] == "verified"
        and report.get("reserve_satisfied", False)
        and report["view"] in ("unmounted", "verified_read_only")
    )
    return report


def state_healthy(report: HealthReport) -> bool:
    """Shared state-only health; disk and view checks are reported separately."""
    return not (
        report["state_problems"]
        or report["counts"].get("issues", 0)
        or report["counts"].get("publications", 0)
        or report["attention_files"]
        or report.get("unfinished_jobs", 0)
    )
