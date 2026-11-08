"""Doctor reports actionable offline profile failures with isolated fakes."""

import json
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock, patch

from cloudmarks import doctor
from tests.test_offline import OfflineCase


class DoctorProfileTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.desktop = {
            "version": 4,
            "core_config": str(self.private / "offline.json"),
            "visible_root": self.settings["visible_root"],
            "upload_root": str(self.root / "upload"),
            "upload_socket": str(self.private / "absent.sock"),
            "upload_config": str(self.private / "upload.conf"),
            "socket": str(self.private / "status.sock"),
        }
        self.settings["rclone"] = "/bin/true"
        (self.private / "upload.conf").write_text(
            "[upload]\ntype=onedrive\ndrive_id=test-drive\nroot_folder_id=test-drive#allowed-root\n"
        )
        (self.private / "upload.conf").chmod(0o600)

    def report(self, *, mounted=None, online=False, minimum=None):
        if minimum is not None:
            self.settings["min_free_bytes"] = minimum
        report = doctor.Report()
        with (
            patch("cloudmarks.offline_config.load_offline", return_value=(self.config, self.settings)),
            patch("cloudmarks.offline_config.check_volume"),
            patch("cloudmarks.offline_config.mounted_view", return_value=mounted),
            patch("cloudmarks.onedrive.OneDrive") as transport,
            patch("cloudmarks.controls.profile", return_value=(None, None, {"view_service": "fake.service"})),
            patch("cloudmarks.view.attest_view", return_value=False),
        ):
            doctor.profile_group(
                report,
                self.desktop,
                online=online,
                run=Mock(return_value=SimpleNamespace(stdout="LoadState=loaded\nActiveState=inactive\n")),
            )
        return {v["name"]: v for v in report.checks}, transport

    def test_stopped_mounts_and_low_space_keep_actionable_hints(self):
        checks, transport = self.report(minimum=2**100)
        self.assertEqual(checks["Offline store"]["status"], "warn")
        self.assertIn("Connect", checks["Offline store"]["hint"])
        self.assertEqual(checks["Offline view"]["status"], "warn")
        self.assertIn("systemctl --user start", checks["Offline view"]["hint"])
        self.assertEqual(checks["Upload view"]["status"], "warn")
        self.assertEqual(checks["Status helper"]["status"], "warn")
        transport.return_value.authenticate.assert_not_called()
        self.assertEqual(checks["Offline state"]["status"], "ok")

    def test_foreign_mounts_and_invalid_upload_socket_fail_closed(self):
        self.desktop["upload_socket"] = str(self.private / "upload.conf")
        checks, transport = self.report(mounted={"type": "ext4"})
        self.assertEqual(checks["Offline view"]["status"], "fail")
        self.assertEqual(checks["Upload view"]["status"], "fail")
        self.assertEqual(checks["Upload status socket"]["status"], "fail")
        self.assertIn("Start", checks["Upload status socket"]["hint"])
        checks, transport = self.report(mounted={"type": "fuse.rclone"}, online=True)
        self.assertEqual(checks["Upload view"]["status"], "ok")
        transport.return_value.authenticate.assert_called_once()

    def test_mismatched_profile_reports_failure_before_any_transport(self):
        report = doctor.Report()
        with (
            patch(
                "cloudmarks.offline_config.load_offline",
                return_value=(self.config, dict(self.settings, visible_root="/another/view")),
            ),
            patch("cloudmarks.onedrive.OneDrive") as transport,
        ):
            doctor.profile_group(report, self.desktop, online=False, run=Mock())
        self.assertEqual(len(report.checks), 1)
        self.assertTrue(report.failed)
        transport.assert_not_called()

    def test_service_probe_failure_is_skipped_and_inactive_has_start_hint(self):
        report = doctor.Report()
        doctor.services_checks(
            report,
            which=lambda name: "/fake/systemctl",
            run=Mock(side_effect=subprocess.TimeoutExpired("fake", 1)),
        )
        self.assertTrue(all(v["status"] == "skip" for v in report.checks))
        report = doctor.Report()
        doctor.services_checks(
            report,
            which=lambda name: "/fake/systemctl",
            run=Mock(return_value=SimpleNamespace(stdout="LoadState=loaded\nActiveState=inactive\n")),
        )
        self.assertTrue(all(v["status"] == "warn" and "systemctl" in v["hint"] for v in report.checks))

    def test_tuple_failure_keeps_hint_and_invalid_report_status_is_rejected(self):
        report = doctor.Report()
        report.attempt("Test", "view", lambda: ("fail", "Not mounted"), "Start the view service")
        self.assertEqual(report.checks[0]["hint"], "Start the view service")
        self.assertIn("Start the view service", report.render())
        with self.assertRaises(ValueError):
            report.add("Test", "invalid", "invalid", "detail")
        self.assertNotIn(str(self.root), json.dumps(report.as_json()))
