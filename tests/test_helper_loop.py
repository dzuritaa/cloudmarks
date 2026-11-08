"""Real Unix peers stress framing, admission, identity and listener cleanup."""

import os
import socket
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from cloudmarks.helper import serve
from cloudmarks.ipc import MAX_FRAME, UNKNOWN, encode, receive, request
from cloudmarks.model import ConfigurationError


class HelperLoopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cm-loop-", dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.path = self.base / "status.sock"
        self.settings = {"socket": str(self.path), "mode": "fixture"}
        self.stop, self.ready = threading.Event(), threading.Event()
        self.errors, self.calls = [], []
        self.handler = lambda value: {"version": 1, "results": {p: UNKNOWN for p in value["paths"]}}
        self.thread = None

    def start(self):
        owner = self

        class Service:
            def handle(self, value):
                owner.calls.append(threading.get_ident())
                return owner.handler(value)

        def run():
            try:
                serve(self.settings, None, service=Service(), stop=self.stop.is_set, ready=self.ready.set)
            except Exception as error:
                self.errors.append(error)
                self.ready.set()

        self.thread = threading.Thread(target=run)
        self.thread.start()
        self.addCleanup(self.close)
        self.assertTrue(self.ready.wait(2))
        self.assertFalse(self.errors)

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(2)
            self.assertFalse(self.thread.is_alive())
        self.assertFalse(self.errors)

    def connect(self, frame=b"", *, may_close=False):
        peer = socket.socket(socket.AF_UNIX)
        self.addCleanup(peer.close)
        peer.settimeout(2)
        peer.connect(str(self.path))
        if frame:
            try:
                peer.sendall(frame)
            except (BrokenPipeError, ConnectionResetError):
                # An over-capacity peer may be closed before it writes.
                if not may_close:
                    raise
        return peer

    def test_idle_and_fragmented_clients_do_not_block_healthy_peers(self):
        self.handler = lambda value: {
            "version": 1,
            "results": {p: dict(UNKNOWN, state="cloud_only") for p in value["paths"]},
        }
        self.start()
        idle = self.connect(b"{")
        begin = time.monotonic()
        self.assertEqual(request(self.path, ["file"])["file"]["state"], "cloud_only")
        self.assertLess(time.monotonic() - begin, 0.3)
        idle.sendall(b'"version":1,"paths":["later"]}\n')
        self.assertIn("later", receive(idle)["results"])
        self.assertEqual(set(self.calls), {self.thread.ident})

    def test_bad_frames_eof_and_database_error_leave_listener_alive(self):
        self.start()
        for frame in (b"bad\n", b"{}\n{}\n", b"x" * (MAX_FRAME + 1), b"[]\n"):
            peer = self.connect(frame)
            try:
                self.assertEqual(peer.recv(1), b"")
            except ConnectionResetError:
                pass
            peer.close()
        self.connect().close()
        self.handler = lambda value: (_ for _ in ()).throw(sqlite3.OperationalError("locked"))
        self.assertEqual(request(self.path, ["file"])["file"], UNKNOWN)
        self.handler = lambda value: {"version": 1, "results": {p: UNKNOWN for p in value["paths"]}}
        peer = self.connect(encode({"paths": ["healthy"]}))
        self.assertIn("healthy", receive(peer)["results"])

    def test_admission_and_timeouts_bound_idle_clients_and_shutdown_closes_them(self):
        self.start()
        peers = []
        for _ in range(20):
            peers.append(self.connect(b"{", may_close=True))
            time.sleep(0.005)
        # Overflow closes immediately; admitted peers close at the absolute deadline.
        for peer in peers:
            try:
                self.assertEqual(peer.recv(1), b"")
            except ConnectionResetError:
                pass
        self.assertTrue(self.thread.is_alive())
        remaining = self.connect()
        self.close()
        self.assertEqual(remaining.recv(1), b"")
        self.assertFalse(self.path.exists())

    def test_foreign_uid_is_rejected_before_any_handle(self):
        with patch("cloudmarks.helper.same_user", return_value=False):
            self.start()
            peer = self.connect()
            self.assertEqual(peer.recv(1), b"")
            self.close()
        self.assertFalse(self.calls)

    def test_stale_socket_is_replaced_but_foreign_replacement_survives_shutdown(self):
        with socket.socket(socket.AF_UNIX) as stale:
            stale.bind(str(self.path))
        self.start()
        self.path.unlink()
        self.path.write_text("unrelated replacement")
        self.close()
        self.assertEqual(self.path.read_text(), "unrelated replacement")

    def test_live_listener_lock_and_symlink_collisions_are_actionable(self):
        self.start()
        with self.assertRaisesRegex(ConfigurationError, "already owns"):
            serve(self.settings, None, stop=lambda: True)
        self.close()
        target = self.base / "target"
        target.write_text("preserve")
        (self.base / "helper.lock").unlink()
        (self.base / "helper.lock").symlink_to(target)
        with self.assertRaises(OSError):
            serve(self.settings, None, stop=lambda: True)
        self.assertEqual(target.read_text(), "preserve")

    def test_active_listener_is_not_replaced_even_without_cloudmarks_lock(self):
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(self.path))
            listener.listen()
            with self.assertRaisesRegex(ConfigurationError, "live listener"):
                serve(self.settings, None, stop=lambda: True)
        self.assertTrue(self.path.exists())

    def test_invalid_lock_type_refuses_without_removing_it(self):
        lock = self.base / "helper.lock"
        os.mkfifo(lock, 0o600)
        with self.assertRaisesRegex(ConfigurationError, "Invalid helper lock"):
            serve(self.settings, None, stop=lambda: True)
        self.assertTrue(lock.exists())


class HelperLifecycleTests(unittest.TestCase):
    def test_account_workers_close_even_when_socket_loop_fails(self):
        from unittest.mock import Mock

        from cloudmarks import helper

        for version in (2, 4):
            settings = {"mode": "onedrive", "version": version, "core_config": "/fake/config"}
            with (
                patch("cloudmarks.offline_config.load_offline", return_value=(Mock(), {})),
                patch("cloudmarks.state.ReadStore"),
                patch("cloudmarks.activity.Activity") as activity,
                patch("cloudmarks.folders.FolderObserver") as folders,
                patch.object(helper, "OfflineStatusService") as service,
                patch.object(helper, "serve", side_effect=OSError("closed")),
            ):
                with self.assertRaises(OSError):
                    helper.run(settings)
                service.return_value.close.assert_called_once()
                if version == 4:
                    activity.return_value.start.return_value.close.assert_called_once()
                    folders.return_value.start.return_value.close.assert_called_once()
                else:
                    activity.assert_not_called()
                    folders.assert_not_called()

    def test_fixture_run_owns_writer_only_for_artificial_settings(self):
        from cloudmarks import helper

        with (
            patch.object(helper, "load_config"),
            patch.object(helper, "StateStore"),
            patch.object(helper, "serve") as serve_mock,
        ):
            helper.run({"mode": "fixture", "core_config": "/fake/config"})
        serve_mock.assert_called_once()
