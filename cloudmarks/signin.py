"""Setup's sign-in: rclone browser authorization and a minimal read-only Graph client.

Tokens are parsed without being echoed and are written only to the private
sign-in copy. Requests never follow redirects and responses are size-bounded.
"""

import base64
import configparser
import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

from .install import write_new
from .model import ConfigurationError, ScopeError, StorageError

ORIGIN_KEYS = ("type", "drive_id", "drive_type", "token", "root_folder_id", "client_id", "client_secret")
# rclone's default OneDrive scopes plus User.Read, which /me needs to confirm the
# signed-in account's email on every authentication.
SCOPES = (
    "Files.Read Files.ReadWrite Files.Read.All Files.ReadWrite.All Sites.Read.All offline_access User.Read"
)


class SetupGraph:
    """Minimal read-only Graph client over the freshly created sign-in copy."""

    def __init__(self, rclone, config_path, *, opener=None, run=subprocess.run):
        self.rclone, self.config_path, self.run = rclone, Path(config_path), run
        self.opener = opener or urllib.request.build_opener(_NoRedirect())

    def token(self):
        from .onedrive import private_config

        parser = private_config(self.config_path)
        try:
            value = json.loads(parser["origin"]["token"])["access_token"]
        except (KeyError, ValueError, TypeError):
            raise ConfigurationError("Sign-in did not produce a usable OneDrive credential") from None
        if not isinstance(value, str) or not value or any(ord(c) < 33 for c in value):
            raise ConfigurationError("Sign-in did not produce a usable OneDrive credential")
        return value

    def get(self, route, *, refreshed=False):
        request = urllib.request.Request(
            "https://graph.microsoft.com/v1.0/" + route, headers={"Authorization": "Bearer " + self.token()}
        )
        try:
            with self.opener.open(request, timeout=20) as stream:
                data = stream.read(1024 * 1024 + 1)
            if len(data) > 1024 * 1024:
                raise StorageError("OneDrive response exceeded its size limit")
            return json.loads(data)
        except urllib.error.HTTPError as error:
            code = error.code
            error.close()
            if code == 401 and not refreshed:
                from .onedrive import clean_environment

                self.run(
                    [self.rclone, "lsjson", "origin:", "--stat", "--config", str(self.config_path)],
                    env=clean_environment(),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=45,
                )
                return self.get(route, refreshed=True)
            if code == 404:
                raise ScopeError("That OneDrive folder does not exist") from None
            raise StorageError(f"OneDrive returned HTTP {code}") from None
        except (OSError, ValueError) as error:
            raise StorageError("OneDrive could not be reached; check the connection") from error


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def authorize_blob():
    """Extra `rclone authorize` settings, encoded the way rclone decodes them."""
    data = json.dumps({"access_scopes": SCOPES}, separators=(",", ":")).encode()
    return base64.b64encode(data).decode().rstrip("=")


def authorized_token(output):
    """Extract the token from `rclone authorize` output, without echoing it.

    With extra settings rclone prints an encoded settings map containing the
    token; without them it prints the token JSON itself.
    """
    match = re.search(r"--->\s*\n(.*?)\n\s*<---End paste", output or "", re.S)
    try:
        if match is None:
            raise ValueError
        printed = match.group(1).strip()
        if not printed.startswith("{"):
            settings = json.loads(base64.b64decode(printed + "=" * (-len(printed) % 4), validate=True))
            printed = settings["token"]
        value = json.loads(printed)
        if not isinstance(value, dict) or not isinstance(value.get("access_token"), str):
            raise ValueError
    except (AttributeError, ValueError, KeyError, TypeError):
        raise StorageError("OneDrive sign-in did not complete; run setup again to retry") from None
    return printed


def write_origin(path, origin, *, replace=False):
    """Write the private sign-in copy; replacing goes through a new file and rename."""
    parser = configparser.ConfigParser(interpolation=None)
    parser["origin"] = origin
    data = _ini(parser)
    if not replace:
        write_new(path, data)
        return
    temporary = path.with_name(path.name + ".new")
    temporary.unlink(missing_ok=True)
    write_new(temporary, data)
    os.replace(temporary, path)


def _ini(parser):
    from io import StringIO

    stream = StringIO()
    parser.write(stream)
    return stream.getvalue().encode()
