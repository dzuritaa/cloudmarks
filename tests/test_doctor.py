import json
import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from cloudmarks.doctor import Report, probe_helper, profile_checks, rclone_version, system_checks
from cloudmarks.model import ConfigurationError


def runner(version="rclone v1.75.2"):
    return lambda argv, **kwargs: SimpleNamespace(returncode=0, stdout=version + "\n")


class DoctorSystemTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.rclone = self.root / "rclone"
        self.rclone.write_text("#!/bin/sh\n")
        self.rclone.chmod(0o755)

    def libraries(self):
        for relative in (
            "lib/x86_64-linux-gnu/nautilus/extensions-4/libnautilus-python.so",
            "lib/x86_64-linux-gnu/girepository-1.0/Nautilus-4.1.typelib",
            "lib/x86_64-linux-gnu/girepository-1.0/Gtk-4.0.typelib",
        ):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("")
        return (str(self.root / "lib"),)

    def which(self, name):
        return {"rclone": str(self.rclone), "fusermount3": "/bin/true", "systemctl": "/bin/true"}.get(name)

    def statuses(self, **kwargs):
        report = Report()
        system_checks(report, **kwargs)
        return {check["name"]: check["status"] for check in report.checks}

    def test_complete_system_passes(self):
        result = self.statuses(which=self.which, run=runner(), roots=self.libraries())
        self.assertEqual(set(result.values()), {"ok"}, result)

    def test_missing_rclone_and_fuse_fail_but_missing_nautilus_only_warns(self):
        result = self.statuses(which=lambda name: None, run=runner(), roots=(str(self.root / "none"),))
        self.assertEqual(result["rclone"], "fail")
        self.assertEqual(result["FUSE"], "fail")
        self.assertEqual(result["Nautilus Python loader"], "warn")
        self.assertEqual(result["GTK 4 introspection"], "warn")

    def test_untested_rclone_versions_warn(self):
        for version in ("rclone v1.60.0", "rclone v1.80.1"):
            result = self.statuses(which=self.which, run=runner(version), roots=self.libraries())
            self.assertEqual(result["rclone"], "warn", version)

    def test_unreadable_version_is_reported(self):
        with self.assertRaises(ValueError):
            rclone_version("rclone", runner("something else"))
        result = self.statuses(which=self.which, run=runner("garbage"), roots=self.libraries())
        self.assertEqual(result["rclone"], "fail")


class DoctorReportTests(unittest.TestCase):
    def test_failures_set_exit_status_and_render_hints(self):
        report = Report()
        report.add("Group", "First", "ok", "Fine")
        report.add("Group", "Second", "fail", "Broken", "Fix it")
        self.assertTrue(report.failed)
        text = report.render()
        self.assertIn("[FAIL] Second: Broken", text)
        self.assertIn("-> Fix it", text)
        self.assertEqual(report.as_json()["counts"]["fail"], 1)

    def test_unexpected_exceptions_hide_their_messages(self):
        report = Report()
        report.attempt("Group", "Leaky", lambda: (_ for _ in ()).throw(OSError("/private/path/secret")))
        report.attempt("Group", "Expected", lambda: (_ for _ in ()).throw(ConfigurationError("Safe message")))
        self.assertNotIn("/private/path", json.dumps(report.as_json()))
        self.assertEqual(report.checks[1]["detail"], "Safe message")

    def test_missing_installation_suggests_setup(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": directory, "XDG_DATA_HOME": directory}):
                report = Report()
                profile_checks(report, None)
        self.assertEqual(report.checks[0]["status"], "warn")
        self.assertEqual(report.checks[0]["hint"], "Run cloudmarks setup")


class HelperProbeTests(unittest.TestCase):
    def test_probe_accepts_a_responding_helper_and_rejects_bad_frames(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            os.chmod(directory, 0o700)
            path = Path(directory) / "status.sock"
            for reply, ok in ((b'{"version":1,"results":{}}\n', True), (b'{"version":9}\n', False)):
                server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                server.bind(str(path))
                server.listen(1)

                def serve(server=server, reply=reply):
                    connection, _ = server.accept()
                    with connection:
                        connection.recv(4096)
                        connection.sendall(reply)

                thread = threading.Thread(target=serve)
                thread.start()
                try:
                    if ok:
                        probe_helper(path)
                    else:
                        with self.assertRaises(ValueError):
                            probe_helper(path)
                finally:
                    thread.join(5)
                    server.close()
                    path.unlink()

    def test_missing_socket_raises_file_not_found(self):
        with tempfile.TemporaryDirectory() as directory:
            os.chmod(directory, 0o700)
            with self.assertRaises(FileNotFoundError):
                probe_helper(Path(directory) / "status.sock")


if __name__ == "__main__":
    unittest.main()
