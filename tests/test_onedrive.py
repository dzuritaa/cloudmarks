"""Transport scope, metadata integrity, mount identity and read-only IPC."""

import base64
import configparser
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cloudmarks.config import Config
from cloudmarks.helper import OfflineStatusService
from cloudmarks.ipc import validate_settings
from cloudmarks.model import ConfigurationError, IntegrityError, ScopeError, StorageError
from cloudmarks.offline_config import check_binding, check_volume, initialize, mounted_view, store_binding
from cloudmarks.onedrive import OneDrive, QuickXor
from cloudmarks.state import ReadStore
from tests.test_offline import OfflineCase


class GraphTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cm-graph-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = Config(
            "test-drive",
            "allowed-root",
            ("blocked",),
            self.root / "store",
            "vol",
            self.root / "state.sqlite3",
        )
        self.settings = {
            "rclone_config": str(self.root / "transport.conf"),
            "rclone": "/usr/bin/rclone",
            "expected_email": "test@example.invalid",
        }
        self.parser = configparser.ConfigParser(interpolation=None)
        self.parser["origin"] = {
            "type": "onedrive",
            "drive_id": "test-drive",
            "drive_type": "personal",
            "root_folder_id": "test-drive#allowed-root",
            "token": json.dumps({"access_token": "artificial"}),
        }
        self.parser["view"] = {
            "type": "union",
            "upstreams": f'"{self.config.storage_root}/local:ro" origin::ro',
            "search_policy": "epall",
        }
        self.save()
        self.backend = OneDrive(self.config, self.settings)
        self.calls = []
        value = QuickXor()
        value.update(b"fixture")
        self.file = {
            "id": "item-id",
            "name": "document.txt",
            "size": 7,
            "eTag": "revision-1",
            "file": {"hashes": {"quickXorHash": base64.b64encode(value.digest()).decode()}},
            "parentReference": {"driveId": "test-drive", "id": "allowed-root"},
        }
        self.folder = {
            "id": "allowed-root",
            "name": "allowed",
            "folder": {},
            "parentReference": {"driveId": "test-drive", "id": "drive-root", "path": "/drive/root:"},
        }

        def graph(route):
            self.calls.append(route)
            if route.startswith("me/drive?"):
                return {"id": "test-drive"}
            if route.startswith("me?"):
                return {"mail": "test@example.invalid"}
            if route.endswith("/items/allowed-root"):
                return self.folder
            if route.endswith("/items/drive-root"):
                return {
                    "id": "drive-root",
                    "folder": {},
                    "root": {},
                    "parentReference": {"driveId": "test-drive"},
                }
            if ":/" in route:
                return self.file
            raise ScopeError("Unproven ancestry")

        self.backend.graph = graph

    def save(self):
        with Path(self.settings["rclone_config"]).open("w") as stream:
            self.parser.write(stream)
        Path(self.settings["rclone_config"]).chmod(0o600)

    def test_scoped_item_and_server_checksum(self):
        value = self.backend.describe("document.txt")
        self.assertEqual(value.remote.identity.item_id, "item-id")
        self.assertEqual(value.kind, "quickXorHash")
        self.assertEqual(value.remote.size, 7)
        self.assertTrue(
            all(route.startswith(("me?", "me/drive?", "drives/test-drive/items/")) for route in self.calls)
        )

    def test_wrong_user_wrong_drive_shortcuts_and_drive_root_are_rejected(self):
        original = self.backend.graph
        self.backend.graph = lambda route: (
            {"mail": "someone-else@example.invalid"} if route.startswith("me?") else original(route)
        )
        with self.assertRaises(ScopeError):
            self.backend.describe("document.txt")
        self.backend.graph = original
        for change in (
            {"remoteItem": {}},
            {"root": {}},
            {"parentReference": {"driveId": "other-drive", "id": "outside"}},
        ):
            with self.subTest(change=change):
                original_folder = dict(self.folder)
                self.folder.update(change)
                with self.assertRaises((ScopeError, ConfigurationError)):
                    self.backend.describe("document.txt")
                self.folder = original_folder

    def test_file_ancestry_name_and_type_are_enforced(self):
        original = json.loads(json.dumps(self.file))
        for change in (
            {"name": "different.txt"},
            {"remoteItem": {}},
            {"folder": {}},
            {"parentReference": {"driveId": "other-drive", "id": "allowed-root"}},
            {"parentReference": {"driveId": "test-drive", "id": "outside-root"}},
        ):
            self.file = json.loads(json.dumps(original))
            self.file.update(change)
            with self.assertRaises((ScopeError, ConfigurationError)):
                self.backend.describe("document.txt")

    def test_no_checksum_or_malformed_checksum_never_creates_evidence(self):
        for hashes in ({}, {"quickXorHash": "garbage"}, {"quickXorHash": "AAAA"}, {"sha1Hash": "nothex"}):
            self.file["file"]["hashes"] = hashes
            with self.assertRaises(IntegrityError):
                self.backend.describe("document.txt")

    def test_unsafe_paths_rejected_before_any_request(self):
        for path in ("../outside", "/absolute", "a//b", "x:y", "x\\y"):
            with self.assertRaises(ScopeError):
                self.backend.describe(path)
        self.assertEqual(self.calls, [])

    def test_effective_rclone_item_identity_is_checked_separately(self):
        observation = self.backend.describe("document.txt")
        value = {"ID": "test-drive#item-id", "IsDir": False, "Size": 7}
        with patch(
            "cloudmarks.onedrive.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, json.dumps(value).encode()),
        ):
            self.backend.verify_transport_item(observation)
        for change in (
            {"ID": "test-drive#outside-item"},
            {"ID": "other-drive#item-id"},
            {"IsDir": True},
            {"Size": 8},
        ):
            bad = dict(value, **change)
            with patch(
                "cloudmarks.onedrive.subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, json.dumps(bad).encode()),
            ):
                with self.assertRaises(ScopeError):
                    self.backend.verify_transport_item(observation)

    def test_transport_roots_and_mutable_upstream_policies_cannot_be_overridden(self):
        for key, value in (
            ("drive_id", "other-drive"),
            ("root_folder_id", "raw-item-id"),
            ("token_url", "https://invalid.example/"),
        ):
            original = dict(self.parser["origin"])
            self.parser["origin"][key] = value
            self.save()
            with self.assertRaises((ConfigurationError, ScopeError)):
                self.backend.validate_transport()
            self.parser["origin"] = original
        self.parser["view"]["search_policy"] = "ff"
        self.save()
        with self.assertRaises(ScopeError):
            self.backend.validate_transport()

    def test_malformed_transport_diagnostics_do_not_quote_credential_lines(self):
        Path(self.settings["rclone_config"]).write_text("[origin]\nfixture-secret-without-equals\n")
        with self.assertRaises(ConfigurationError) as caught:
            self.backend.validate_transport()
        self.assertNotIn("fixture-secret", str(caught.exception))

    def test_invalid_credential_never_reaches_http_or_raw_diagnostics(self):
        for value in ("fixture-secret-not-json", "[]", '{"access_token":"fixture-secret\\nheader"}'):
            self.parser["origin"]["token"] = value
            self.save()
            with patch.object(
                self.backend.opener, "open", side_effect=AssertionError("Must not send invalid credentials")
            ):
                with self.assertRaises(ConfigurationError) as caught:
                    OneDrive.graph(self.backend, "me/drive?$select=id")
                self.assertNotIn("fixture-secret", str(caught.exception))


class VolumeTests(unittest.TestCase):
    def test_automount_trigger_is_not_mistaken_for_accessed_storage_device(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = Config("drive", "root", ("blocked",), root, "vol", root / "state.sqlite3")
            dev = os.stat(root).st_dev
            row = {
                "uuid": "expected-volume",
                "target": "/data",
                "fstype": "ntfs3",
                "options": "rw,nodev",
                "maj:min": f"{os.major(dev)}:{os.minor(dev)}",
            }
            response = {
                "filesystems": [
                    {
                        "uuid": None,
                        "target": "/data",
                        "fstype": "autofs",
                        "options": "rw",
                        "maj:min": "0:999999",
                    },
                    row,
                ]
            }
            with (
                patch(
                    "cloudmarks.offline_config.subprocess.run",
                    return_value=subprocess.CompletedProcess([], 0, json.dumps(response).encode()),
                ),
                patch("cloudmarks.offline_config.check_binding"),
            ):
                check_volume(
                    config, {"storage_mount_uuid": "expected-volume", "storage_mount_target": "/data"}
                )
                with self.assertRaises(StorageError):
                    check_volume(
                        config, {"storage_mount_uuid": "wrong-volume", "storage_mount_target": "/data"}
                    )

    def test_store_binding_prevents_a_second_database_or_view_from_adopting_data(self):
        from dataclasses import replace

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            config = Config("drive", "root", ("blocked",), base / "new-store", "vol", base / "state.sqlite3")
            settings = {"visible_root": str(base / "view")}
            with (
                patch("cloudmarks.offline_config.check_volume"),
                patch("cloudmarks.offline_config.require_unmounted"),
            ):
                initialize(config, settings)
            check_binding(config, settings)
            self.assertEqual(
                json.loads((config.storage_root / ".cloudmarks-binding").read_text()),
                store_binding(config, settings),
            )
            for changed in (
                replace(config, state_path=base / "other.sqlite3"),
                replace(config, allowed_root_id="other-root"),
            ):
                with self.assertRaises(StorageError):
                    check_binding(changed, settings)
            with self.assertRaises(StorageError):
                check_binding(config, {"visible_root": str(base / "other-view")})

    def test_mountinfo_octal_escapes_are_decoded(self):
        from io import StringIO

        record = "44 1 0:42 / /private/a\\040b ro,nosuid - fuse.rclone cloudmarks-test ro\n"
        with patch("builtins.open", return_value=StringIO(record)):
            value = mounted_view({"visible_root": "/private/a b"})
        self.assertEqual(value["id"], "44")
        self.assertIn("ro", value["options"])

    def test_version_two_status_configuration_does_not_add_actions(self):
        value = {
            "version": 2,
            "mode": "onedrive",
            "enabled": True,
            "core_config": "/private/offline.json",
            "visible_root": "/private/view",
            "socket": "/run/user/test.sock",
        }
        self.assertEqual(validate_settings(value), value)
        with self.assertRaises(ConfigurationError):
            validate_settings(dict(value, version=1))


class StatusReaderTests(OfflineCase):
    def test_read_only_service_refuses_mutation_and_does_not_open_transport(self):
        self.keep()
        desktop = {"visible_root": self.settings["visible_root"]}
        with ReadStore(self.config) as reader:
            service = OfflineStatusService(desktop, reader, self.settings)
            for op in ("keep", "delete", "free", "unpin", "cancel"):
                with self.assertRaises(ValueError):
                    service.handle({"version": 1, "op": op, "paths": [self.path]})
            with patch("cloudmarks.view.check_volume", side_effect=StorageError("No disk")):
                self.assertEqual(service.lookup(self.path)["state"], "unknown")

    def test_reader_reports_abandoned_writer_without_writing_database(self):
        self.keep()
        self.store.begin_transfer(self.identity)
        self.store.close()
        with ReadStore(self.config) as reader:
            service = OfflineStatusService(
                {"visible_root": self.settings["visible_root"]}, reader, self.settings
            )
            result = service.lookup(self.path)
            self.assertEqual(result["state"], "error")
            self.assertEqual(reader.row(self.identity)["operation"], "active")

    def test_status_proof_never_opens_the_user_visible_fuse_path(self):
        from cloudmarks.view import device_name, prove_view

        copy = self.keep()
        remote = self.store.remote(self.identity)
        record = {"mount_id": "42", "storage": str(self.disk), "volume": self.config.volume_id}
        mounted = {"id": "42", "source": device_name(self.config), "options": {"ro"}, "type": "fuse.rclone"}
        with (
            patch("cloudmarks.view.check_volume"),
            patch("cloudmarks.view.read_private_json", return_value=record),
            patch("cloudmarks.view.mounted_view", return_value=mounted),
            patch("builtins.open", side_effect=AssertionError("Must not open a cloud/FUSE path")),
        ):
            self.assertTrue(prove_view(self.config, self.settings, remote, copy))
            mounted["options"] = {"rw"}
            self.assertFalse(prove_view(self.config, self.settings, remote, copy))

    def test_unsafe_lock_cannot_block_the_display_helper(self):
        self.keep()
        self.store.begin_transfer(self.identity)
        self.store.close()
        lock = Path(str(self.config.state_path) + ".lock")
        lock.unlink()
        os.mkfifo(lock)
        with ReadStore(self.config) as reader:
            service = OfflineStatusService(
                {"visible_root": self.settings["visible_root"]}, reader, self.settings
            )
            with self.assertRaises(ConfigurationError):
                service.lookup(self.path)

    def test_online_mount_probe_persists_new_revision_instead_of_blessing_old_bytes(self):
        from cloudmarks.engine import StatusEngine
        from cloudmarks.view import refresh_online_probe

        copy = self.keep()
        old = self.visible().read_bytes()
        self.next_version()
        self.backend.verify_transport_item = lambda observation: None
        refresh_online_probe(self.config, self.store, self.backend)
        self.assertEqual(self.store.remote(self.identity).revision, self.backend.revision)
        self.assertEqual(
            StatusEngine(self.config, self.store, read_path=lambda *args: True)
            .status(self.identity)
            .state.value,
            "unknown",
        )
        self.assertEqual(self.store.copy(self.identity), copy)
        self.assertEqual(self.visible().read_bytes(), old)

    def test_changed_checksum_with_same_revision_is_not_refreshed_as_success(self):
        from cloudmarks.view import refresh_online_probe

        copy = self.keep()
        self.backend.content = b"changed" + self.backend.content[7:]
        self.backend.verify_transport_item = lambda observation: None
        with self.assertRaises(IntegrityError):
            refresh_online_probe(self.config, self.store, self.backend)
        self.assertEqual(self.store.row(self.identity)["operation"], "error")
        self.assertEqual(self.store.copy(self.identity), copy)

    def test_replaced_online_probe_identity_preserves_and_marks_old_snapshot(self):
        from cloudmarks.view import refresh_online_probe

        copy = self.keep()
        self.backend.item = "another-item"
        self.backend.verify_transport_item = lambda observation: None
        with self.assertRaises(ScopeError):
            refresh_online_probe(self.config, self.store, self.backend)
        self.assertEqual(self.store.row(self.identity)["operation"], "error")
        self.assertEqual(self.store.copy(self.identity), copy)

    def test_schema_one_upgrades_transactionally_without_changing_existing_copy(self):
        copy = self.keep()
        self.store.connection.executescript(
            "DROP TABLE local_entries; DROP TABLE publications; PRAGMA user_version=1;"
        )
        self.reopen()
        self.assertEqual(self.store.connection.execute("PRAGMA user_version").fetchone()[0], 5)
        self.assertEqual(self.store.copy(self.identity), copy)
        self.assertTrue(self.store.row(self.identity)["pinned"])


class BatchTests(GraphTests):
    def test_batches_correlate_unordered_results_beyond_old_library_limit(self):
        original = self.backend.graph
        batches = []

        def graph(route, **kwargs):
            if route != "$batch":
                return original(route)
            rows = kwargs["payload"]["requests"]
            batches.append(rows)
            responses = []
            for row in rows:
                name = row["url"].rsplit("/", 1)[-1].removesuffix(":")
                body = dict(self.file, id=name, name=name)
                responses.append({"id": row["id"], "status": 200, "body": body})
            return {"responses": list(reversed(responses))}

        self.backend.graph = graph
        paths = [f"file-{i}.txt" for i in range(140)]
        values = self.backend.describe_many(paths)
        self.assertEqual(set(values), set(paths))
        self.assertEqual(len(batches), 7)
        self.assertTrue(
            all(
                row["url"].startswith("/drives/test-drive/root:/allowed/") for rows in batches for row in rows
            )
        )
        self.assertTrue(
            all(len(rows) == 20 and all(row["method"] == "GET" for row in rows) for rows in batches)
        )

    def test_failed_item_is_unknown_and_misplaced_success_is_rejected(self):
        original = self.backend.graph
        self.backend.graph = lambda route, **kwargs: (
            {"responses": [{"id": "0", "status": 404}]} if route == "$batch" else original(route)
        )
        self.assertEqual(self.backend.describe_many(["document.txt"]), {"document.txt": None})
        self.backend.graph = lambda route, **kwargs: (
            {"responses": [{"id": "0", "status": 200, "body": dict(self.file, name="wrong.txt")}]}
            if route == "$batch"
            else original(route)
        )
        with self.assertRaises(ScopeError):
            self.backend.describe_many(["document.txt"])

    def test_actual_transport_rejects_mutations_and_noncanonical_batch_urls(self):
        backend = OneDrive(self.config, self.settings)
        backend._batch_prefix = "/drives/test-drive/root:/allowed/"
        for method, url in (
            ("DELETE", "/drives/test-drive/root:/allowed/document.txt"),
            ("GET", "/drives/test-drive/items/outside:/document.txt"),
            ("GET", "/drives/test-drive/root:/allowed/document.txt?expand=content"),
            ("GET", "/drives/test-drive/root:/allowed/%2e%2e/secret"),
        ):
            with self.subTest(method=method, url=url), self.assertRaises(ScopeError):
                backend.graph("$batch", payload={"requests": [{"id": "0", "method": method, "url": url}]})

    def test_account_attestation_memo_expires_and_credential_changes_invalidate_it(self):
        with patch("cloudmarks.onedrive.time.monotonic", return_value=100) as clock:
            self.backend.authenticate()
            first = len(self.calls)
            clock.return_value = 110
            self.backend.authenticate()
            self.assertEqual(len(self.calls), first)
            clock.return_value = 121
            self.backend.authenticate()
            self.assertEqual(len(self.calls), first * 2)
            self.parser["origin"]["token"] = json.dumps({"access_token": "artificial-new"})
            self.save()
            self.backend.authenticate()
            self.assertEqual(len(self.calls), first * 3)
