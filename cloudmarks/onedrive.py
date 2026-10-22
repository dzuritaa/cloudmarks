"""Read-only Graph identity/metadata and scoped rclone content transport.

Credentials are explicit private configuration. No cloud write/delete operation
is implemented. Downloaded bytes must match a server checksum and stable item.
"""

import base64
import configparser
import hashlib
import json
import os
import selectors
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .ipc import private_directory
from .model import ConfigurationError, IntegrityError, RemoteFile, ScopeError, StorageError, canonical_drive
from .paths import relative_parts
from .telemetry import error_category, record


class RemoteMissing(StorageError):
    """Authenticated stable ID returned 404; absence is not proof of deletion."""


class Cancelled(StorageError):
    pass


class QuickXor:
    def __init__(self):
        self.data = bytearray(20)
        self.length = 0
        self.shift = 0

    def update(self, content):
        data, shift = self.data, self.shift
        for value in content:
            index, bits = divmod(shift, 8)
            data[index] ^= (value << bits) & 255
            if bits:
                data[(index + 1) % 20] ^= value >> (8 - bits)
            shift = (shift + 11) % 160
        self.shift = shift
        self.length += len(content)

    def digest(self):
        result = bytearray(self.data)
        for index, value in enumerate(self.length.to_bytes(8, "little")):
            result[12 + index] ^= value
        return bytes(result)


def checksum(kind):
    if kind == "quickXorHash":
        return QuickXor()
    if kind == "sha1Hash":
        return hashlib.sha1()  # Service checksum, not a signature/security primitive.
    if kind == "sha256Hash":
        return hashlib.sha256()
    raise IntegrityError("No supported authoritative remote checksum")


@dataclass(frozen=True)
class Observation:
    remote: RemoteFile
    kind: str
    digest: bytes

    def same_version(self, other):
        a, b = self.remote, other.remote
        return (
            a.identity,
            a.root_id,
            a.relative_path,
            a.revision,
            a.size,
            a.deleted,
            a.is_directory,
            self.kind,
            self.digest,
        ) == (
            b.identity,
            b.root_id,
            b.relative_path,
            b.revision,
            b.size,
            b.deleted,
            b.is_directory,
            other.kind,
            other.digest,
        )


def private_config(path):
    path = Path(path)
    parent = private_directory(path.parent)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, "rb") as stream:
            import stat

            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
                or info.st_size > 1024 * 1024
            ):
                raise ConfigurationError("Transport configuration must be a private regular user-owned file")
            data = stream.read(1024 * 1024 + 1).decode()
        value = configparser.ConfigParser(interpolation=None)
        value.read_string(data)
        return value
    except (configparser.Error, UnicodeError, ValueError):
        # ConfigParser errors can quote the offending line, which may contain
        # credentials. Never let those raw exceptions reach CLI diagnostics.
        raise ConfigurationError("Transport configuration could not be parsed safely") from None
    finally:
        os.close(parent)


def clean_environment():
    return {k: v for k, v in os.environ.items() if not k.startswith("RCLONE_")}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


class OneDrive:
    def __init__(self, config, settings):
        self.config, self.settings = config, settings
        self.opener = urllib.request.build_opener(NoRedirect())
        self.validate_transport()

    def validate_transport(self):
        parser = private_config(self.settings["rclone_config"])
        if set(parser.sections()) != {"origin", "view"}:
            raise ConfigurationError("Expected the dedicated origin/view transport configuration")
        origin = parser["origin"]
        allowed = {"type", "drive_id", "drive_type", "token", "root_folder_id", "client_id", "client_secret"}
        if (
            set(origin) - allowed
            or origin.get("type") != "onedrive"
            or canonical_drive(origin.get("drive_id")) != self.config.expected_drive_id
            or origin.get("root_folder_id")
            != self.config.expected_drive_id + "#" + self.config.allowed_root_id
        ):
            raise ScopeError("Transport does not match the expected account and allowed root")
        expected = f'"{self.config.storage_root}/local:ro" origin::ro'
        if dict(parser["view"]) != {"type": "union", "upstreams": expected, "search_policy": "epall"}:
            raise ScopeError("View must be the exact local-first, read-only union")
        return parser

    def argv(self, *arguments):
        self.validate_transport()
        return [
            self.settings["rclone"],
            *arguments,
            "--config",
            self.settings["rclone_config"],
            "--cache-dir",
            str(self.config.state_path.parent / "rclone-cache"),
            "--retries=1",
            "--low-level-retries=1",
            "--contimeout=5s",
            "--timeout=15s",
        ]

    def graph(self, route, *, refreshed=False):
        # Callers build route components with quote(..., safe=''). Metadata GETs
        # never follow redirects carrying the bearer token to another host.
        parser = self.validate_transport()
        try:
            credential = json.loads(parser["origin"]["token"])
            token = credential.get("access_token") if isinstance(credential, dict) else None
            if (
                not isinstance(token, str)
                or not 1 <= len(token) <= 16384
                or any(ord(char) < 33 or ord(char) == 127 for char in token)
            ):
                raise ValueError("Invalid credential")
        except (ValueError, TypeError, KeyError):
            raise ConfigurationError(
                "Transport credential is unavailable; reauthenticate the test account locally"
            ) from None
        request = urllib.request.Request(
            "https://graph.microsoft.com/v1.0/" + route, headers={"Authorization": "Bearer " + token}
        )
        try:
            with self.opener.open(request, timeout=20) as stream:
                data = stream.read(1024 * 1024 + 1)
                if len(data) > 1024 * 1024:
                    raise StorageError("Metadata response exceeded its size limit")
                return json.loads(data)
        except urllib.error.HTTPError as error:
            code = error.code
            error.close()
            record("transport", "metadata_failed", status_code=code)
            if code == 401 and not refreshed:
                # Refresh only this experiment's credential copy via a GET-only
                # rclone operation. Never reconnect or edit the daily remote.
                result = subprocess.run(
                    self.argv("lsjson", "origin:", "--stat"),
                    env=clean_environment(),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=45,
                )
                if result.returncode == 0:
                    return self.graph(route, refreshed=True)
            if code == 404:
                raise RemoteMissing("Remote item is unavailable; preserve retained snapshots") from None
            raise StorageError("Remote metadata unavailable; retain local evidence") from None
        except (OSError, ValueError) as error:
            record("transport", "metadata_failed", error=error_category(error))
            raise StorageError("Remote metadata unavailable; retain local evidence") from error

    def item(self, item_id):
        q = urllib.parse.quote
        return self.graph(f"drives/{q(self.config.expected_drive_id, safe='')}/items/{q(item_id, safe='')}")

    def authenticate(self):
        try:
            self._authenticate()
        except RemoteMissing:
            raise StorageError("Account or allowed root is unavailable; retain local evidence") from None

    def _authenticate(self):
        drive = self.graph("me/drive?$select=id")
        me = self.graph("me?$select=mail,userPrincipalName")
        if canonical_drive(drive["id"]) != self.config.expected_drive_id or self.settings[
            "expected_email"
        ].casefold() not in {str(me.get(k, "")).casefold() for k in ("mail", "userPrincipalName")}:
            raise ScopeError("Authenticated user/drive is not the configured test account")
        root = self.item(self.config.allowed_root_id)
        if (
            root.get("id") != self.config.allowed_root_id
            or "folder" not in root
            or "remoteItem" in root
            or "root" in root
        ):
            raise ScopeError("Allowed root identity is not a local folder in this drive")
        parent = root.get("parentReference", {})
        if canonical_drive(parent.get("driveId")) != self.config.expected_drive_id:
            raise ScopeError("Allowed root belongs to another drive")

    def describe(self, path, *, allow_directory=False):
        relative_parts(path)
        self.authenticate()
        q = urllib.parse.quote
        value = self.graph(
            f"drives/{q(self.config.expected_drive_id, safe='')}/items/"
            f"{q(self.config.allowed_root_id, safe='')}:/{q(path, safe='/')}"
        )
        return self.observation(value, path, allow_directory=allow_directory)

    def describe_identity(self, identity):
        self.config.validate_identity(identity)
        self.authenticate()
        try:
            value = self.item(identity.item_id)
        except RemoteMissing:
            self.authenticate()  # Root/account must still be accessible.
            raise
        if value.get("id") != identity.item_id:
            raise ScopeError("Stable item lookup returned another identity")
        return self.observation(value, allow_directory=True)

    def observation(self, value, path=None, *, allow_directory=False, ancestors=None):
        initial = value
        names = []
        seen = set()
        for _ in range(64):
            if "remoteItem" in value or "deleted" in value or value.get("id") in seen:
                raise ScopeError("Shared, deleted or cyclic item is outside the supported root")
            seen.add(value["id"])
            parent = value.get("parentReference", {})
            if canonical_drive(parent.get("driveId")) != self.config.expected_drive_id:
                raise ScopeError("Item ancestry left the expected drive")
            names.append(value["name"])
            if parent.get("id") == self.config.allowed_root_id:
                break
            # A bounded catalog already established each traversed parent's
            # identity and ancestry. Reuse only that scan's verified responses;
            # individual identity/path controls still perform fresh lookups.
            if ancestors is None:
                value = self.item(parent["id"])
            else:
                try:
                    value = ancestors[parent["id"]]
                except KeyError:
                    raise ScopeError("Catalog ancestry was not established in this scan") from None
            if "folder" not in value:
                raise ScopeError("Item ancestry is not a folder")
        else:
            raise ScopeError("Allowed-root ancestry could not be established")
        resolved = "/".join(reversed(names))
        if path is not None and resolved != path:
            raise ScopeError("Resolved item does not match the requested path")
        relative_parts(resolved)
        if "folder" in initial and "file" not in initial and allow_directory:
            from .model import FileIdentity

            remote = RemoteFile(
                FileIdentity(self.config.expected_drive_id, initial["id"]),
                self.config.allowed_root_id,
                resolved,
                initial["eTag"],
                0,
                time.time(),
                is_directory=True,
            )
            self.config.validate_remote(remote)
            return Observation(remote, "", b"")
        if "file" not in initial or "folder" in initial:
            raise ScopeError("Resolved item is not a supported file")
        hashes = initial["file"].get("hashes", {})
        for kind, length in (("sha256Hash", 32), ("sha1Hash", 20), ("quickXorHash", 20)):
            if kind not in hashes:
                continue
            try:
                digest = (
                    base64.b64decode(hashes[kind], altchars=b"-_", validate=True)
                    if kind == "quickXorHash"
                    else bytes.fromhex(hashes[kind])
                )
            except ValueError:
                raise IntegrityError("Malformed remote checksum") from None
            if len(digest) != length:
                raise IntegrityError("Malformed remote checksum length")
            from .model import FileIdentity

            remote = RemoteFile(
                FileIdentity(self.config.expected_drive_id, initial["id"]),
                self.config.allowed_root_id,
                resolved,
                initial["eTag"],
                initial["size"],
                time.time(),
                digest.hex() if kind == "sha256Hash" else None,
            )
            self.config.validate_remote(remote)
            return Observation(remote, kind, digest)
        raise IntegrityError("Remote file has no supported authoritative checksum")

    def catalog(self, *, limit=128, cancelled=lambda: False):
        """Observe the allowed root without pinning or inferring any deletions.

        A failed/truncated scan returns no catalog. Credentials and continuation
        requests remain confined to Graph and the exact verified children route.
        """
        if type(limit) is not int or not 1 <= limit <= 128:
            raise ScopeError("Catalog limit must be between 1 and 128")
        self.authenticate()
        q = urllib.parse.quote
        pending = [(self.config.allowed_root_id, "", 0)]
        values, seen, paths, ancestors = [], set(), set(), {}
        deadline, pages = time.monotonic() + 60, 0
        while pending:
            item_id, parent_path, depth = pending.pop()
            prefix = (
                f"drives/{q(self.config.expected_drive_id, safe='')}/items/{q(item_id, safe='')}/children"
            )
            route, visited = prefix + "?$top=128", set()
            while route:
                if cancelled():
                    raise Cancelled("Catalog cancelled")
                if time.monotonic() > deadline or pages >= 32 or route in visited:
                    raise StorageError("Catalog exceeded its time/page bound")
                visited.add(route)
                pages += 1
                page = self.graph(route)
                if not isinstance(page, dict) or not isinstance(page.get("value"), list):
                    raise IntegrityError("Incomplete catalog")
                for value in page["value"]:
                    if len(seen) >= limit or depth >= 16:
                        raise StorageError("Catalog exceeded its item/depth bound")
                    # Reject misplaced entries before consulting the scan-local
                    # parent map, including a response claiming another folder.
                    if value.get("parentReference", {}).get("id") != item_id:
                        raise ScopeError("Catalog child belongs to another parent")
                    child = self.observation(value, allow_directory=True, ancestors=ancestors)
                    remote = child.remote
                    expected_path = (parent_path + "/" if parent_path else "") + value["name"]
                    if (
                        value.get("parentReference", {}).get("id") != item_id
                        or remote.relative_path != expected_path
                        or remote.identity in seen
                        or self.config.path_key(remote.relative_path) in paths
                    ):
                        raise ScopeError("Catalog has duplicate or misplaced children")
                    seen.add(remote.identity)
                    paths.add(self.config.path_key(remote.relative_path))
                    name = value["name"]
                    if remote.is_directory and (
                        name == ".Trash" or name.startswith(".Trash-") and name[7:].isdigit()
                    ):
                        # GNOME may move deleted mount files into these hidden
                        # folders. Observe no trash contents and never remove it.
                        continue
                    values.append(child)
                    if remote.is_directory:
                        ancestors[remote.identity.item_id] = value
                        pending.append((remote.identity.item_id, remote.relative_path, depth + 1))
                route = None
                if (link := page.get("@odata.nextLink")) is not None:
                    if not isinstance(link, str) or len(link) > 8192 or any(ord(c) < 33 for c in link):
                        raise ScopeError("Invalid catalog continuation")
                    parsed = urllib.parse.urlsplit(link)
                    if (
                        parsed.scheme != "https"
                        or parsed.netloc != "graph.microsoft.com"
                        or parsed.path != "/v1.0/" + prefix
                        or not parsed.query
                        or parsed.fragment
                    ):
                        raise ScopeError("Catalog continuation left the verified children endpoint")
                    route = prefix + "?" + parsed.query
        return values

    def tree(self, path, *, limit=128, cancelled=lambda: False):
        """Bounded complete manifest, independently repeated by the controller.

        Never infer deletions from listing omissions. Pages must stay on the
        exact children endpoint; bearer credentials cannot follow arbitrary URLs.
        """
        if type(limit) is not int or not 1 <= limit <= 128:
            raise ScopeError("Recursive manifest limit must be between 1 and 128")
        root = self.describe(path, allow_directory=True)
        if not root.remote.is_directory:
            raise ScopeError("Recursive intent requires a folder")
        q = urllib.parse.quote
        values, pending, seen, paths = (
            [root],
            [(root, 0)],
            {root.remote.identity},
            {self.config.path_key(path)},
        )
        deadline, pages = time.monotonic() + 120, 0
        while pending:
            folder, depth = pending.pop()
            prefix = (
                f"drives/{q(self.config.expected_drive_id, safe='')}/items/"
                f"{q(folder.remote.identity.item_id, safe='')}/children"
            )
            route, visited = prefix + "?$top=200", set()
            while route:
                if cancelled():
                    raise Cancelled("Folder enumeration cancelled; prior intent retained")
                if time.monotonic() > deadline or pages >= 32 or route in visited:
                    raise StorageError("Recursive enumeration exceeded its time/page bound")
                pages += 1
                visited.add(route)
                page = self.graph(route)
                if not isinstance(page, dict) or not isinstance(page.get("value"), list):
                    raise IntegrityError("Incomplete recursive listing")
                for value in page["value"]:
                    if len(values) >= limit or depth >= 16:
                        raise StorageError("Recursive enumeration exceeded its item/depth bound")
                    child = self.observation(value, allow_directory=True)
                    remote = child.remote
                    parent = value.get("parentReference", {})
                    if (
                        parent.get("id") != folder.remote.identity.item_id
                        or remote.relative_path.rsplit("/", 1)[0] != folder.remote.relative_path
                        or remote.identity in seen
                        or self.config.path_key(remote.relative_path) in paths
                    ):
                        raise ScopeError("Recursive listing has duplicate or misplaced children")
                    seen.add(remote.identity)
                    paths.add(self.config.path_key(remote.relative_path))
                    values.append(child)
                    if remote.is_directory:
                        pending.append((child, depth + 1))
                link = page.get("@odata.nextLink")
                route = None
                if link is not None:
                    if not isinstance(link, str) or len(link) > 8192 or any(ord(c) < 33 for c in link):
                        raise ScopeError("Invalid continuation link")
                    parsed = urllib.parse.urlsplit(link)
                    if (
                        parsed.scheme != "https"
                        or parsed.netloc != "graph.microsoft.com"
                        or parsed.path != "/v1.0/" + prefix
                        or not parsed.query
                        or parsed.fragment
                    ):
                        raise ScopeError("Continuation left the verified children endpoint")
                    route = prefix + "?" + parsed.query
        return values

    def verify_transport_item(self, observation):
        self.config.validate_remote(observation.remote)
        try:
            result = subprocess.run(
                self.argv("lsjson", "origin:" + observation.remote.relative_path, "--stat"),
                env=clean_environment(),
                capture_output=True,
                timeout=45,
            )
            if result.returncode or len(result.stdout) > 1024 * 1024:
                raise StorageError("Cannot establish the effective transport file identity")
            value = json.loads(result.stdout)
            drive, item = value.get("ID", "").split("#", 1)
            if (
                canonical_drive(drive) != observation.remote.identity.drive_id
                or item != observation.remote.identity.item_id
                or value.get("IsDir") is not False
                or value.get("Size") != observation.remote.size
            ):
                raise ScopeError("Effective transport item does not match scoped Graph evidence")
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            raise StorageError("Cannot establish the effective transport file identity") from None

    def chunks(self, observation, *, cancelled=lambda: False):
        # A path race cannot publish different content: the caller checks the
        # server checksum and re-describes identity/revision after EOF.
        if cancelled():
            raise Cancelled("Download cancelled before transport started")
        self.verify_transport_item(observation)
        if cancelled():
            raise Cancelled("Download cancelled before transport started")
        process = subprocess.Popen(
            self.argv("cat", "origin:" + observation.remote.relative_path),
            env=clean_environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        deadline = time.monotonic() + 30
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while True:
                    if cancelled():
                        raise Cancelled("Download cancelled; previous complete data was retained")
                    if not selector.select(0.1):
                        if time.monotonic() > deadline:
                            raise StorageError("Transport stalled; previous data was retained")
                        continue
                    block = os.read(process.stdout.fileno(), 256 * 1024)
                    if not block:
                        break
                    deadline = time.monotonic() + 30
                    yield block
            if process.wait(timeout=5):
                raise StorageError("Transport did not complete; previous data was retained")
        except Exception as error:
            record("transport", "download_failed", error=error_category(error))
            raise
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
            process.stdout.close()
