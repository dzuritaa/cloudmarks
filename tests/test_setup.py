import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from cloudmarks.doctor import run_doctor
from cloudmarks.model import ConfigurationError, ScopeError, StorageError
from cloudmarks.setup import (
    STATUS_UNIT,
    UPLOAD_UNIT,
    VIEW_UNIT,
    Layout,
    Prompter,
    Setup,
    load_answers,
    render_units,
    unit_word,
)

DRIVE = "aabbccddeeff0011"
FOLDER = "AABBCCDDEEFF0011!123"
EMAIL = "test@example.invalid"


class FakeGraph:
    calls = []

    def __init__(self, rclone, config_path, routes):
        self.routes = routes

    def get(self, route):
        FakeGraph.calls.append(route)
        for prefix, value in self.routes.items():
            if route.startswith(prefix):
                return value
        raise ScopeError("That OneDrive folder does not exist")


def routes(**overrides):
    value = {
        "me?": {"mail": EMAIL},
        "me/drive?": {"id": DRIVE.upper(), "driveType": "personal"},
        "me/drive/root/children": {"value": [{"id": "x", "name": "Test Folder", "folder": {}}]},
        "me/drive/root:/Test%20Folder?": {
            "id": FOLDER,
            "name": "Test Folder",
            "folder": {"childCount": 0},
            "parentReference": {"driveId": DRIVE},
        },
    }
    value.update(overrides)
    return value


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="cm-setup-", dir="/tmp")
        self.root = Path(self.temporary.name)
        environment = {
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "XDG_DATA_HOME": str(self.root / "data"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_CACHE_HOME": str(self.root / "cache"),
            "XDG_RUNTIME_DIR": str(self.root / "run"),
        }
        patcher = mock.patch.dict(os.environ, environment)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.temporary.cleanup)
        for name in ("config", "data", "state", "cache", "run"):
            (self.root / name).mkdir(mode=0o700)
        self.rclone = self.root / "bin/rclone"
        self.rclone.parent.mkdir()
        self.rclone.write_text("#!/bin/sh\nexit 0\n")
        self.rclone.chmod(0o755)
        self.commands = []
        self.signins = 0
        self.answers = {
            "accept_experimental": True,
            "account_email": EMAIL,
            "folder": "Test Folder",
            "store_parent": str(self.root / "disk/offline"),
            "upload_root": str(self.root / "views/Upload"),
            "offline_root": str(self.root / "views/Offline"),
            "budget_gib": 1,
            "reserve_gib": 1,
        }
        FakeGraph.calls = []

    def which(self, name):
        return {"rclone": str(self.rclone), "fusermount3": "/bin/true", "systemctl": "/bin/true"}.get(name)

    def fake_run(self, argv, **kwargs):
        argv = [str(part) for part in argv]
        self.commands.append(argv)
        if argv[1:] == ["version"]:
            return SimpleNamespace(returncode=0, stdout="rclone v1.75.2\n")
        if argv[1:3] == ["config", "create"]:
            self.signins += 1
            target = Path(argv[argv.index("--config") + 1])
            target.write_text(
                "[origin]\ntype = onedrive\n"
                'token = {"access_token":"fixture-access","refresh_token":"fixture-refresh"}\n'
                f"drive_id = {DRIVE}\ndrive_type = personal\n"
            )
            target.chmod(0o600)
            return SimpleNamespace(returncode=0, stdout="")
        return SimpleNamespace(returncode=0, stdout="")

    def initializer(self, config, values):
        from cloudmarks.offline_config import initialize
        from cloudmarks.state import StateStore

        with mock.patch("cloudmarks.offline_config.check_volume"):
            initialize(config, values)
            with StateStore(config):
                pass

    def setup(self, answers=None, graph=None, initializer=None):
        return Setup(
            Prompter(self.answers if answers is None else answers, interactive=False),
            layout=Layout.current(),
            run=self.fake_run,
            which=self.which,
            graph_factory=lambda rclone, path: FakeGraph(rclone, path, graph or routes()),
            volume=lambda path: ("fixture-uuid", str(self.root)),
            store_initializer=initializer or self.initializer,
            say=lambda *args: None,
        )

    def test_complete_setup_produces_a_profile_every_component_accepts(self):
        report = self.setup().execute()
        layout = Layout.current()
        for name in ("core.toml", "transport.conf", "upload.conf", "offline.json", "controls.json"):
            info = layout.file(name).stat()
            self.assertEqual(stat.S_IMODE(info.st_mode), 0o600, name)
        self.assertFalse(layout.file("signin.conf").exists(), "temporary sign-in copy must be removed")
        checks = {check["name"]: check for check in report.checks}
        for name in ("Account and storage configuration", "Transport configuration", "Upload configuration"):
            self.assertEqual(checks[name]["status"], "ok", checks[name])
        self.assertEqual(checks["Menu controls"]["status"], "ok", checks["Menu controls"])
        self.assertNotIn("live_updates", json.loads(layout.file("controls.json").read_text()))
        self.assertEqual(checks["Installed files"]["status"], "ok", checks["Installed files"])
        settings = json.loads((layout.config / "desktop.json").read_text())
        self.assertTrue(settings["enabled"])
        self.assertEqual(settings["version"], 4)
        core = (layout.file("core.toml")).read_text()
        self.assertIn(f'expected_drive_id = "{DRIVE.upper()}"', core)
        self.assertIn("blocked_drive_ids = []", core)
        transport = layout.file("transport.conf").read_text()
        self.assertIn(f"root_folder_id = {DRIVE.upper()}#{FOLDER}", transport)
        for unit in (UPLOAD_UNIT, STATUS_UNIT, VIEW_UNIT):
            self.assertTrue((layout.units / unit).exists(), unit)
        self.assertIn(["systemctl", "--user", "daemon-reload"], self.commands)
        self.assertIn(
            ["systemctl", "--user", "enable", "--now", UPLOAD_UNIT, STATUS_UNIT, VIEW_UNIT], self.commands
        )
        record = json.loads(layout.file("setup.json").read_text())
        self.assertEqual(
            record["completed"], ["account", "folder", "storage", "files", "store", "install", "services"]
        )
        for path in (self.answers["upload_root"], self.answers["offline_root"]):
            self.assertEqual(stat.S_IMODE(Path(path).stat().st_mode), 0o700)

    def test_live_updates_are_opt_in(self):
        self.setup(dict(self.answers, live_updates=True)).execute()
        controls = json.loads(Layout.current().file("controls.json").read_text())
        self.assertTrue(controls["live_updates"])

    def test_doctor_report_is_free_of_paths_ids_and_email(self):
        self.setup().execute()
        report = run_doctor(run=self.fake_run, which=self.which)
        text = report.render() + json.dumps(report.as_json())
        for secret in (str(self.root), DRIVE, DRIVE.upper(), FOLDER, EMAIL, "fixture-access", "fixture-uuid"):
            self.assertNotIn(secret, text)

    def test_interrupted_setup_resumes_without_signing_in_again(self):
        failing = mock.Mock(side_effect=StorageError("disk unavailable"))
        with self.assertRaises(StorageError):
            self.setup(initializer=failing).execute()
        record = json.loads(Layout.current().file("setup.json").read_text())
        self.assertEqual(record["completed"], ["account", "folder", "storage", "files"])
        self.setup().execute()
        self.assertEqual(self.signins, 1)

    def test_service_start_failure_is_reported_and_resumable(self):
        def failing(argv, **kwargs):
            if "enable" in argv:
                raise subprocess.CalledProcessError(1, argv)
            return self.fake_run(argv, **kwargs)

        setup = self.setup()
        setup.run = failing
        with self.assertRaises(StorageError):
            setup.execute()
        record = json.loads(Layout.current().file("setup.json").read_text())
        self.assertNotIn("services", record["completed"])
        self.setup().execute()
        self.assertIn(
            ["systemctl", "--user", "enable", "--now", UPLOAD_UNIT, STATUS_UNIT, VIEW_UNIT], self.commands
        )

    def test_existing_installation_is_never_replaced(self):
        config = Path(os.environ["XDG_CONFIG_HOME"]) / "cloudmarks"
        config.mkdir(mode=0o700)
        (config / "desktop.json").write_text("{}")
        with self.assertRaises(ConfigurationError):
            self.setup().execute()
        self.assertEqual(self.signins, 0)

    def test_experimental_acknowledgement_is_required(self):
        answers = dict(self.answers)
        del answers["accept_experimental"]
        with self.assertRaises(ConfigurationError):
            self.setup(answers).execute()
        self.assertFalse(Layout.current().profile.exists())

    def test_wrong_account_and_shared_folder_are_refused(self):
        with self.assertRaises(ScopeError):
            self.setup(dict(self.answers, account_email="someone-else@example.invalid")).execute()
        shutil.rmtree(Layout.current().profile)
        shortcut = routes(**{"me/drive/root:/Test%20Folder?": {"id": FOLDER, "folder": {}, "remoteItem": {}}})
        with self.assertRaises(ScopeError):
            self.setup(graph=shortcut).execute()

    def test_drive_root_is_refused(self):
        with self.assertRaises(ConfigurationError):
            self.setup(dict(self.answers, folder="/")).execute()

    def test_nonempty_view_folder_is_refused(self):
        view = Path(self.answers["offline_root"])
        view.mkdir(parents=True)
        (view / "pending.txt").write_text("keep me")
        with self.assertRaises(StorageError):
            self.setup().execute()
        self.assertEqual((view / "pending.txt").read_text(), "keep me")

    def test_answers_file_rejects_unknown_keys(self):
        path = self.root / "answers.toml"
        path.write_text('folder = "Test Folder"\ntoken = "nope"\n')
        with self.assertRaises(ConfigurationError):
            load_answers(path)
        path.write_text('folder = "Test Folder"\naccept_experimental = true\n')
        self.assertEqual(load_answers(path)["folder"], "Test Folder")

    def test_dry_run_plan_changes_nothing(self):
        before = sorted(self.root.rglob("*"))
        self.assertIn("nothing has been changed", self.setup().plan())
        self.assertEqual(sorted(self.root.rglob("*")), before)


class UnitTests(unittest.TestCase):
    def test_unit_words_are_quoted_and_escaped(self):
        self.assertEqual(unit_word("/plain/path"), "/plain/path")
        self.assertEqual(unit_word("/with space/x"), '"/with space/x"')
        self.assertEqual(unit_word('/q"uote'), '"/q\\"uote"')
        self.assertEqual(unit_word("/100%/$HOME"), '"/100%%/$$HOME"')
        with self.assertRaises(ConfigurationError):
            unit_word("/bad\nnewline")

    def paths(self, base):
        return {
            "desktop": f"{base}/config/cloudmarks/desktop.json",
            "helper": f"{base}/data/cloudmarks/m2/run.py",
            "offline": f"{base}/My Profile/offline.json",
            "mount_env": f"{base}/config/cloudmarks/profile/mount.env",
            "upload_root": f"{base}/CloudMarks/My Upload",
            "upload_config": f"{base}/profile/upload.conf",
            "upload_cache": f"{base}/cache/upload",
            "upload_socket": "/run/user/1000/cloudmarks-upload/rc.sock",
            "reserve_gib": 20,
        }

    def test_units_order_services_and_guard_both_views(self):
        units = render_units(self.paths("/home/u"), python="/usr/bin/python3", rclone="/usr/bin/rclone")
        self.assertIn(f"After={UPLOAD_UNIT}", units[STATUS_UNIT])
        self.assertIn(f"After={STATUS_UNIT}", units[VIEW_UNIT])
        self.assertIn("--operation prepare", units[UPLOAD_UNIT])
        self.assertIn("--view offline --operation protect", units[VIEW_UNIT])
        self.assertIn("--rc-addr=unix:///run/user/1000/cloudmarks-upload/rc.sock", units[UPLOAD_UNIT])
        self.assertIn('"/home/u/CloudMarks/My Upload"', units[UPLOAD_UNIT])

    @unittest.skipUnless(shutil.which("systemd-analyze"), "systemd-analyze unavailable")
    def test_generated_units_pass_systemd_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            rclone = Path(directory) / "rclone"
            rclone.write_text("#!/bin/sh\n")
            rclone.chmod(0o755)
            units = render_units(self.paths(directory), python=sys.executable, rclone=str(rclone))
            for name, text in units.items():
                (Path(directory) / name).write_text(text)
            result = subprocess.run(
                ["systemd-analyze", "--user", "verify", *(str(Path(directory) / n) for n in units)],
                capture_output=True,
                text=True,
                timeout=60,
            )
            own = [line for line in result.stderr.splitlines() if directory in line]
            self.assertEqual(own, [])


if __name__ == "__main__":
    unittest.main()
