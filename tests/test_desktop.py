"""Read-only IPC, deadline, scope and reversible deployment acceptance checks."""

import compileall
import json
import os
import queue
import socket
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from cloudmarks import install
from cloudmarks.desktop import MAX_TRACKED, Background
from cloudmarks.fixture_session import create
from cloudmarks.helper import MAX_VERIFY_BYTES, StatusService, serve
from cloudmarks.ipc import (
    MAX_BATCH,
    MAX_FRAME,
    UNKNOWN,
    encode,
    load_settings,
    private_directory,
    receive,
    relative_uri,
    request,
    validate_settings,
)
from cloudmarks.model import ConfigurationError, FileIdentity, ScopeError
from cloudmarks.paths import MARKER


class FixtureCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cm-m2-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings, self.store = create(self.root / "session")
        self.addCleanup(self.store.close)
        self.service = StatusService(self.settings, self.store)
        self.view = Path(self.settings["visible_root"])

    def identity(self, name):
        return FileIdentity(self.store.config.expected_drive_id, name)


class ServiceTests(FixtureCase):
    def test_fixture_states_come_from_engine(self):
        expected = {
            "01-cloud.txt": "cloud_only",
            "02-local.txt": "saved_locally",
            "03-pinned.txt": "kept_offline",
            "04-transfer.txt": "transferring",
            "05-error.txt": "error",
            "06-unknown.txt": "unknown",
            "07-refresh.txt": "saved_locally",
            "08-no-observation.txt": "unknown",
        }
        response = self.service.handle({"version": 1, "op": "status", "paths": list(expected)})
        self.assertEqual({p: r["state"] for p, r in response["results"].items()}, expected)
        self.store.set_pin(self.identity("07-refresh.txt"), True)
        self.assertEqual(self.service.lookup("07-refresh.txt")["state"], "kept_offline")

    def test_visible_bytes_and_marker_are_required_for_green(self):
        path = self.view / "02-local.txt"
        path.chmod(0o600)
        path.write_bytes(b"changed")
        self.assertNotIn(self.service.lookup(path.name)["state"], ("saved_locally", "kept_offline"))
        (self.view / MARKER).unlink()
        self.assertEqual(self.service.lookup("03-pinned.txt")["state"], "unknown")

    def test_symlink_view_cannot_prove_local_availability(self):
        name = "02-local.txt"
        content = (self.view / name).read_bytes()
        outside = self.root / "outside"
        outside.write_bytes(content)
        (self.view / name).unlink()
        (self.view / name).symlink_to(outside)
        self.assertEqual(self.service.lookup(name)["state"], "unknown")

    def test_aliases_and_missing_disk_are_unknown(self):
        self.assertEqual(self.service.lookup("02-LOCAL.txt")["state"], "unknown")
        (self.store.config.storage_root / MARKER).unlink()
        self.assertEqual(self.service.lookup("01-cloud.txt")["state"], "unknown")

    def test_no_remote_observation_or_folder_aggregation(self):
        remote = self.store.remote(self.identity("01-cloud.txt"))
        self.store.observe(replace(remote, is_directory=True))
        self.assertEqual(self.service.lookup("01-cloud.txt")["state"], "unknown")
        self.assertEqual(self.service.lookup("Large folder")["state"], "unknown")

    def test_large_file_is_not_hashed_on_request(self):
        remote = self.store.remote(self.identity("02-local.txt"))
        self.store.observe(replace(remote, size=MAX_VERIFY_BYTES + 1))
        with patch.object(self.service.engine, "status", side_effect=AssertionError("must not hash")):
            self.assertEqual(self.service.lookup(remote.relative_path)["state"], "unknown")

    def test_strict_request_schema_blocks_actions_and_escapes(self):
        cases = [
            None,
            {},
            {"version": True, "op": "status", "paths": ["01-cloud.txt"]},
            {"version": 2, "op": "status", "paths": ["01-cloud.txt"]},
            {"version": 1, "op": "download", "paths": ["01-cloud.txt"]},
            {"version": 1, "op": "status", "paths": ["x"] * (MAX_BATCH + 1)},
            {"version": 1, "op": "status", "paths": ["a", "a"]},
        ]
        for value in cases:
            with self.subTest(value=value), self.assertRaises((ValueError, ScopeError)):
                self.service.handle(value)
        for path in ("../outside", "/etc/passwd", "a//b", "x\\y", "\ud800", 4):
            with self.subTest(path=path), self.assertRaises(ScopeError):
                self.service.handle({"version": 1, "op": "status", "paths": [path]})

    def test_nonfixture_core_binding_is_rejected(self):
        self.store.config = replace(self.store.config, expected_drive_id="another-account")
        with self.assertRaises(ConfigurationError):
            StatusService(self.settings, self.store)

    def test_unknown_evidence_has_time_to_reach_ui_with_reason(self):
        result = self.service.lookup("06-unknown.txt")
        self.assertEqual(result["state"], "unknown")
        self.assertEqual(result["valid_for_ms"], 1000)
        self.assertIn("needs checking", result["reason"])

    def test_runtime_foreign_file_is_preserved(self):
        path = Path(self.settings["socket"])
        path.write_text("preserve")
        with self.assertRaises(ConfigurationError):
            serve(self.settings, self.store, stop=lambda: True)
        self.assertEqual(path.read_text(), "preserve")

    def test_work_budget_yields_unknown_instead_of_unbounded_batch(self):
        with patch("cloudmarks.helper.time.monotonic", side_effect=[0, 1, 2]):
            response = self.service.handle({"version": 1, "op": "status", "paths": ["a", "b"]})
        self.assertTrue(all(r["state"] == "unknown" for r in response["results"].values()))


class SocketTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cm-ipc-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.stop = threading.Event()
        self.ready = threading.Event()
        self.errors = []
        self.edits = queue.Queue()

        def worker():
            try:
                self.settings, store = create(self.root / "session")

                def tick():
                    try:
                        action, completed = self.edits.get_nowait()
                    except queue.Empty:
                        return
                    action(store)
                    completed.set()

                with store:
                    serve(self.settings, store, stop=self.stop.is_set, ready=self.ready.set, tick=tick)
            except Exception as error:
                self.errors.append(error)
                self.ready.set()

        self.thread = threading.Thread(target=worker, daemon=True)
        self.thread.start()
        self.addCleanup(self.shutdown)
        self.assertTrue(self.ready.wait(5), "Helper did not start")
        self.assertFalse(self.errors, self.errors)

    def shutdown(self):
        self.stop.set()
        self.thread.join(3)
        self.assertFalse(self.thread.is_alive(), "Helper failed to stop")
        self.assertFalse(self.errors, self.errors)

    def query(self, *paths):
        return request(self.settings["socket"], list(paths))

    def test_real_socket_refresh_and_shutdown(self):
        self.assertEqual(self.query("07-refresh.txt")["07-refresh.txt"]["state"], "saved_locally")
        event = threading.Event()
        self.edits.put(
            (
                lambda store: store.set_pin(
                    FileIdentity(store.config.expected_drive_id, "07-refresh.txt"), True
                ),
                event,
            )
        )
        self.assertTrue(event.wait(2))
        self.assertEqual(self.query("07-refresh.txt")["07-refresh.txt"]["state"], "kept_offline")
        self.shutdown()
        self.assertFalse(Path(self.settings["socket"]).exists())
        self.assertEqual(self.query("07-refresh.txt")["07-refresh.txt"], UNKNOWN)

    def test_malformed_requests_do_not_crash_helper(self):
        frames = [
            b"not JSON\n",
            encode({"version": 1, "op": "delete", "paths": ["02-local.txt"]}),
            encode({"version": 1, "op": "status", "paths": ["../escape"]}),
            b"x" * (MAX_FRAME + 1),
        ]
        for frame in frames:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(2)
                connection.connect(self.settings["socket"])
                connection.sendall(frame)
                try:
                    connection.recv(1)
                except ConnectionResetError:
                    pass
            self.assertEqual(self.query("02-local.txt")["02-local.txt"]["state"], "saved_locally")

    def test_slow_client_has_a_deadline_and_does_not_poison_next_request(self):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.connect(self.settings["socket"])
            connection.sendall(b"{")
            time.sleep(0.7)
        self.assertEqual(self.query("02-local.txt")["02-local.txt"]["state"], "saved_locally")

    def test_worker_settings_changes_and_fail_closed_responses(self):
        worker = Background(self.root / "session/desktop.json")
        self.addCleanup(worker.close)
        event = worker.outbox.get(timeout=2)
        self.assertEqual(event[0], "settings")
        uri = (Path(self.settings["visible_root"]) / "03-pinned.txt").as_uri()
        worker.submit(uri)
        result = worker.outbox.get(timeout=2)
        self.assertEqual(result[0], "status")
        self.assertEqual(result[-1]["state"], "kept_offline")
        changed = dict(self.settings, enabled=False)
        install.atomic_json(self.root / "session/desktop.json", changed)
        self.assertEqual(worker.outbox.get(timeout=2), ("settings", changed))
        worker.submit(uri)
        self.assertEqual(worker.outbox.get(timeout=2)[-1]["state"], "unknown")
        self.assertLessEqual(worker.inbox.maxsize, MAX_TRACKED)

    def test_socket_permissions_and_untrusted_peer_fail_closed(self):
        path = Path(self.settings["socket"])
        path.chmod(0o666)
        self.assertEqual(self.query("02-local.txt")["02-local.txt"], UNKNOWN)
        path.chmod(0o600)
        with patch("cloudmarks.ipc.same_user", return_value=False):
            self.assertEqual(self.query("02-local.txt")["02-local.txt"], UNKNOWN)


class ProtocolTests(unittest.TestCase):
    def test_uri_scope_and_encoding(self):
        self.assertEqual(relative_uri("file:///fixture/a%20b%25.txt", "/fixture"), "a b%.txt")
        for uri in (
            "file:///fixtures/a",
            "file:///fixture",
            "smb:///fixture/a",
            "file://host/fixture/a",
            "file:///fixture/%2e%2e/private",
            "file:///fixture/a?x",
            "file:///fixture/%FF",
            "file:///fixture/a//b",
        ):
            with self.subTest(uri=uri):
                self.assertIsNone(relative_uri(uri, "/fixture"))

    def test_oversize_and_multiple_frames_are_rejected(self):
        with self.assertRaises(ValueError):
            encode({"x": "a" * MAX_FRAME})
        for data in (b"{}\n{}\n", b"{}\nx", b"x" * (MAX_FRAME + 1)):
            left, right = socket.socketpair()
            with left, right:
                right.sendall(data)
                with self.assertRaises(ValueError):
                    receive(left)

    def test_client_deadline_for_hung_helper(self):
        with tempfile.TemporaryDirectory(prefix="cm-hung-") as directory:
            path = str(Path(directory) / "status.sock")
            with socket.socket(socket.AF_UNIX) as listener:
                listener.bind(path)
                os.chmod(path, 0o600)
                listener.listen(1)
                started = time.monotonic()
                self.assertEqual(request(path, ["item"])["item"], UNKNOWN)
                self.assertLess(time.monotonic() - started, 1.2)

    def test_settings_are_private_fixture_only_and_versioned(self):
        value = {
            "version": 1,
            "mode": "fixture",
            "enabled": True,
            "visible_root": "/fixture",
            "core_config": "/config/core.toml",
            "socket": "/run/user/test.sock",
        }
        self.assertEqual(validate_settings(value), value)
        for changed in (
            dict(value, mode="production"),
            dict(value, version=True),
            dict(value, visible_root="/"),
            dict(value, socket="/a/../socket"),
            dict(value, enabled=1),
            dict(value, unknown=1),
        ):
            with self.assertRaises(ConfigurationError):
                validate_settings(changed)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text(json.dumps(value))
            with self.assertRaises(ConfigurationError):
                load_settings(path)
            path.chmod(0o600)
            self.assertEqual(load_settings(path), value)
            Path(directory).chmod(0o755)
            with self.assertRaises(ConfigurationError):
                private_directory(directory)


class InstallationTests(FixtureCase):
    def setUp(self):
        super().setUp()
        self.env = patch.dict(
            os.environ,
            {"XDG_DATA_HOME": str(self.root / "data"), "XDG_CONFIG_HOME": str(self.root / "config")},
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.source = self.root / "session/desktop.json"

    def test_install_uninstall_before_enable_then_reinstall_disable(self):
        desktop = install.install(self.source)
        self.assertFalse(load_settings(desktop)["enabled"])
        _, _, root, extension, _ = install.inventory()
        self.assertTrue((root / "cloudmarks/icons/hicolor/scalable/emblems/cloudmarks-pin.svg").is_file())
        unrelated = extension.parent / "other_extension.py"
        unrelated.write_text("# unrelated\n")
        install.uninstall()
        self.assertFalse(extension.exists())
        self.assertTrue(unrelated.exists())
        self.assertTrue(self.store.config.storage_root.exists())
        install.install(self.source)
        install.set_enabled(True)
        self.assertTrue(load_settings(desktop)["enabled"])
        install.set_enabled(False)
        self.assertFalse(load_settings(desktop)["enabled"])
        install.uninstall()
        self.assertTrue(self.view.is_dir())

    def test_uninstall_removes_generated_bytecode_and_allows_reinstall(self):
        install.install(self.source)
        _, _, root, extension, _ = install.inventory()
        self.assertTrue(compileall.compile_dir(root, quiet=2))
        install.uninstall()
        self.assertFalse(extension.exists())
        self.assertFalse(root.exists())
        install.install(self.source)
        install.uninstall()

    def test_changed_files_are_preserved_before_any_uninstall(self):
        install.install(self.source)
        _, _, _, extension, _ = install.inventory()
        extension.write_text("# user's changed file\n")
        with self.assertRaises(ConfigurationError):
            install.uninstall()
        self.assertEqual(extension.read_text(), "# user's changed file\n")
        self.assertTrue((self.root / "config/cloudmarks/desktop.json").exists())

    def test_symlinked_deployment_and_foreign_collision_are_preserved(self):
        (self.root / "data").mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        (self.root / "data/cloudmarks").symlink_to(outside)
        with self.assertRaises(OSError):
            install.install(self.source)
        self.assertEqual(list(outside.iterdir()), [])

    def test_duplicate_install_refuses_to_overwrite(self):
        install.install(self.source)
        with self.assertRaises(ConfigurationError):
            install.install(self.source)

    def test_failed_install_removes_partial_files_and_can_be_retried(self):
        original = install.write_new
        calls = 0

        def failing_write(path, data):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise OSError("Artificial full disk")
            original(path, data)

        with patch.object(install, "write_new", side_effect=failing_write), self.assertRaises(OSError):
            install.install(self.source)
        self.assertFalse((self.root / "data/cloudmarks/m2").exists())
        install.install(self.source)
        install.uninstall()

    def test_failed_write_removes_its_new_partial_file(self):
        path = self.root / "partial"
        with patch("cloudmarks.install.os.fsync", side_effect=OSError("Artificial full disk")):
            with self.assertRaises(OSError):
                install.write_new(path, b"incomplete")
        self.assertFalse(path.exists())

    def test_manifest_cannot_remove_files_outside_deployment(self):
        install.install(self.source)
        unrelated = self.root / "precious"
        unrelated.write_text("preserve")
        path = self.root / "config/cloudmarks/install.json"
        value = json.loads(path.read_text())
        value["files"][str(unrelated)] = install.digest(unrelated.read_bytes())
        install.atomic_json(path, value)
        with self.assertRaises(ConfigurationError):
            install.uninstall()
        self.assertTrue(unrelated.exists())
