"""Diagnostic privacy, rate bounds, and unavailable-journal failure behavior."""

import io
import json
import unittest
from unittest.mock import MagicMock, patch

from cloudmarks import telemetry


class TelemetryTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict("os.environ", {"CLOUDMARKS_LOG_LEVEL": "info"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        telemetry._windows.clear()
        self.addCleanup(telemetry._windows.clear)

    def test_only_operational_enums_and_metrics_reach_the_journal(self):
        stream = MagicMock()
        with patch.object(telemetry.socket, "socket", return_value=stream):
            self.assertTrue(
                telemetry.record(
                    "desktop",
                    "status_changed",
                    state="kept_offline",
                    previous_state="unknown",
                    count=1,
                    elapsed_ms=2.5,
                    enabled=True,
                    filename="private-file-canary",
                    path="/private-path-canary",
                    token="token-canary",
                    email="email-canary@example.invalid",
                    reason="content-canary",
                    account_id="account-canary",
                    error="exception-text-canary",
                    mode="unknown-mode-canary",
                )
            )
        data, destination = stream.__enter__.return_value.sendto.call_args.args
        self.assertEqual(destination, "/run/systemd/journal/socket")
        self.assertNotIn("canary", data.decode())
        message = json.loads(
            next(line[8:] for line in data.decode().splitlines() if line.startswith("MESSAGE="))
        )
        self.assertEqual(message["state"], "kept_offline")
        self.assertEqual(message["elapsed_ms"], 2.5)
        self.assertNotIn("filename", message)
        self.assertNotIn("error", message)

    def test_exception_text_and_unknown_event_names_cannot_escape(self):
        error = OSError("sensitive-token-canary /private-name-canary")
        self.assertEqual(telemetry.error_category(error), "socket")
        self.assertIsNone(telemetry.message("desktop", str(error), {}))
        self.assertNotIn(
            "canary",
            json.dumps(
                telemetry.message(
                    "ipc", "ipc_failed", {"error": telemetry.error_category(error), "detail": str(error)}
                )
            ),
        )

    def test_journal_failure_does_not_escape_or_fall_back_to_raw_stderr(self):
        with (
            patch.object(telemetry.socket, "socket", side_effect=OSError("private-error-canary")),
            patch("sys.stderr.write", side_effect=AssertionError("No raw stderr fallback")),
        ):
            self.assertFalse(telemetry.record("bridge", "worker_failed", error="socket"))

    def test_off_and_default_debug_filter_avoid_all_socket_io(self):
        with patch.object(telemetry.socket, "socket", side_effect=AssertionError("No log I/O")):
            self.assertFalse(telemetry.record("helper", "request_completed", level="debug"))
            with patch.dict("os.environ", {"CLOUDMARKS_LOG_LEVEL": "off"}):
                self.assertFalse(telemetry.record("cli", "command_started", command="demo"))

    def test_repeated_events_are_bounded_and_resume_in_the_next_window(self):
        with (
            patch.object(telemetry.socket, "socket"),
            patch.object(telemetry, "_clock", return_value=100) as clock,
        ):
            results = [telemetry.record("ipc", "ipc_failed", error="timeout") for _ in range(100)]
            self.assertEqual(sum(results), 10)
            clock.return_value = 111
            self.assertTrue(telemetry.record("ipc", "ipc_failed", error="timeout"))

    def test_numeric_fields_are_finite_bounded_and_do_not_coerce_objects(self):
        message = telemetry.message(
            "desktop",
            "status_summary",
            {
                "count": True,
                "unknown_count": -1,
                "positive_count": 10**1000,
                "elapsed_ms": float("nan"),
                "remaining_ms": float("inf"),
                "late_ms": 10**1000,
                "exit_code": -15,
            },
        )
        self.assertNotIn("count", message)
        self.assertNotIn("elapsed_ms", message)
        self.assertNotIn("remaining_ms", message)
        self.assertEqual(message["late_ms"], 86400000)
        self.assertEqual(message["exit_code"], -15)

    def test_log_reader_selects_only_the_filtered_current_user_diagnostics(self):
        from cloudmarks.__main__ import main

        with (
            patch("sys.argv", ["cloudmarks", "logs", "--since", "1 hour ago", "--follow"]),
            patch("cloudmarks.__main__.subprocess.call", return_value=0) as run,
        ):
            with self.assertRaises(SystemExit) as exited:
                main()
        self.assertEqual(exited.exception.code, 0)
        command = run.call_args.args[0]
        self.assertIn("--identifier=cloudmarks-diagnostics", command)
        self.assertIn("--user", command)
        self.assertIn("--follow", command)
        self.assertNotIn("--system", command)

    def test_log_reader_rejects_unbounded_output_requests(self):
        from cloudmarks.__main__ import main

        with (
            patch("sys.argv", ["cloudmarks", "logs", "--lines", "100000000"]),
            patch("cloudmarks.__main__.subprocess.call") as run,
            patch("sys.stderr", new_callable=io.StringIO),
        ):
            with self.assertRaises(SystemExit) as exited:
                main()
        self.assertEqual(exited.exception.code, 2)
        run.assert_not_called()
