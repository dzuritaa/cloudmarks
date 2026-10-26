"""Record before/after evidence for the manual physical acceptance tests.

Run phase `before`, perform the physical action from docs/dev/physical-acceptance.md,
then run phase `after` (after logging back in, for the session test). The output
file contains only counts, check names and outcomes: no paths, names, IDs or
credentials. Keep it outside the repository until reviewed.

    python3 tests/acceptance/run_physical.py --test network --phase before --output ~/cm-network.json
    python3 tests/acceptance/run_physical.py --test network --phase after --output ~/cm-network.json
"""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from cloudmarks import __version__  # noqa: E402
from cloudmarks.doctor import default_settings_path, run_doctor  # noqa: E402

TESTS = {
    "network": "Lose and restore the network during a download and a read",
    "disk": "Disconnect and reconnect the offline store's disk",
    "session": "Log out and back in with services enabled",
}


def retained_copies(settings_path):
    """Re-verify every retained copy against its recorded checksum."""
    from cloudmarks.ipc import load_settings
    from cloudmarks.model import FileIdentity
    from cloudmarks.offline import OfflineController
    from cloudmarks.offline_config import load_offline
    from cloudmarks.paths import SecureRoot
    from cloudmarks.state import ReadStore

    settings = load_settings(settings_path)
    config, values = load_offline(settings["core_config"])
    result = {"verified": 0, "failed": 0}
    with ReadStore(config) as store:
        controller = OfflineController(config, store, None, values)
        controller.volume_check(config, values)
        with SecureRoot(config.storage_root, config.volume_id) as root:
            for row in store.connection.execute("SELECT drive_id,item_id FROM local_entries").fetchall():
                try:
                    controller.verify_entry(root, controller.entry(FileIdentity(*row)))
                    result["verified"] += 1
                except Exception:
                    result["failed"] += 1
    return result


def snapshot(settings_path):
    report = run_doctor(settings_path)
    value = {
        "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "doctor": {check["name"]: check["status"] for check in report.checks},
    }
    try:
        value["retained_copies"] = retained_copies(settings_path)
    except Exception as error:
        value["retained_copies"] = {"unavailable": type(error).__name__}
    return value


def compare(before, after):
    findings = []
    copies_before, copies_after = before["retained_copies"], after["retained_copies"]
    if copies_after.get("failed"):
        findings.append("FAIL: retained copies failed verification after the test")
    if "verified" in copies_before and copies_after.get("verified", -1) < copies_before["verified"]:
        findings.append("FAIL: fewer retained copies verify than before")
    for name, status in after["doctor"].items():
        if status == "fail" and before["doctor"].get(name) != "fail":
            findings.append(f"FAIL: doctor check '{name}' now fails")
    return findings or ["PASS: no regressions in retained copies or doctor checks"]


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--test", choices=sorted(TESTS), required=True)
    parser.add_argument("--phase", choices=("before", "after"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--settings", type=Path, default=None)
    args = parser.parse_args()
    settings = args.settings or default_settings_path()
    output = args.output.expanduser()
    if args.phase == "before":
        if output.exists():
            parser.error("--output already exists; choose a new file")
        record = {"version": 1, "cloudmarks": __version__, "test": args.test, "description": TESTS[args.test]}
        record["before"] = snapshot(settings)
        output.write_text(json.dumps(record, indent=2) + "\n")
        output.chmod(0o600)
        print(f"Recorded the 'before' snapshot. Now perform the {args.test} test, then run phase 'after'.")
        return 0
    record = json.loads(output.read_text())
    if record.get("test") != args.test or "after" in record:
        parser.error("Output file belongs to another test or is already complete")
    record["after"] = snapshot(settings)
    observed = input("Did badges, the progress window and open files behave as the checklist expects? [y/N] ")
    record["observer_confirmed_expected_behaviour"] = observed.strip().lower() in ("y", "yes")
    record["findings"] = compare(record["before"], record["after"])
    output.write_text(json.dumps(record, indent=2) + "\n")
    print("\n".join(record["findings"]))
    return 1 if any(f.startswith("FAIL") for f in record["findings"]) else 0


if __name__ == "__main__":
    sys.exit(main())
