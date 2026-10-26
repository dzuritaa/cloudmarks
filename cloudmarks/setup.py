"""Guided, resumable setup of the CloudMarks OneDrive profile for this user.

Setup never overwrites an existing file, never adopts an existing rclone remote
or mount, and never stores a credential outside the private profile folder.
Signing in is delegated to rclone, so passwords and tokens never pass through
CloudMarks output. Re-running continues from the last completed step.
"""

import base64
import configparser
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path

from .install import atomic_json, json_bytes, locations, make_directory, write_new
from .model import ConfigurationError, ScopeError, StorageError, canonical_drive

STEPS = ("account", "folder", "storage", "files", "store", "install", "services")
UPLOAD_UNIT = "cloudmarks-upload.service"
STATUS_UNIT = "cloudmarks-status.service"
VIEW_UNIT = "cloudmarks-view.service"
ORIGIN_KEYS = ("type", "drive_id", "drive_type", "token", "root_folder_id", "client_id", "client_secret")
GIB = 2**30
# rclone's default OneDrive scopes plus User.Read, which /me needs to confirm the
# signed-in account's email on every authentication.
SCOPES = (
    "Files.Read Files.ReadWrite Files.Read.All Files.ReadWrite.All Sites.Read.All offline_access User.Read"
)


def configure(commands):
    parser = commands.add_parser(
        "setup", help="Guided setup: sign in, pick a OneDrive folder, create views and services"
    )
    parser.add_argument("--answers", type=Path, help="TOML answers for a non-interactive setup")
    parser.add_argument(
        "--dry-run", action="store_true", help="Show the planned files without changing anything"
    )


@dataclass
class Layout:
    config: Path
    profile: Path
    data: Path
    state: Path
    cache: Path
    units: Path
    runtime: Path

    @classmethod
    def current(cls):
        data, config = locations()
        base_config = config.parent
        home = Path.home()
        state = Path(os.environ.get("XDG_STATE_HOME", home / ".local/state")) / "cloudmarks"
        cache = Path(os.environ.get("XDG_CACHE_HOME", home / ".cache")) / "cloudmarks"
        runtime = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
        for path in (state, cache, runtime):
            if not path.is_absolute() or ".." in path.parts:
                raise ConfigurationError("XDG directories must be absolute without parent traversal")
        return cls(config, config / "profile", data, state, cache, base_config / "systemd/user", runtime)

    def file(self, name):
        return self.profile / name


class Prompter:
    def __init__(self, answers=None, *, interactive=True, ask=input, say=print):
        self.answers, self.interactive, self.ask, self.say = dict(answers or {}), interactive, ask, say

    def value(self, key, question, default=None, convert=str):
        if key in self.answers:
            return convert(self.answers[key])
        if not self.interactive:
            if default is None:
                raise ConfigurationError(f"Missing setup answer: {key}")
            return convert(default)
        suffix = f" [{default}]" if default is not None else ""
        while True:
            reply = self.ask(f"{question}{suffix}: ").strip()
            if reply or default is not None:
                try:
                    return convert(reply or default)
                except (ValueError, ConfigurationError) as error:
                    self.say(f"  {error}")

    def confirm(self, key, question, default=False):
        if key in self.answers:
            if not isinstance(self.answers[key], bool):
                raise ConfigurationError(f"Setup answer {key} must be true or false")
            return self.answers[key]
        if not self.interactive:
            return default
        reply = self.ask(f"{question} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
        return default if not reply else reply in ("y", "yes")


def unit_word(value):
    """Quote one systemd command-line word; refuse control characters."""
    value = str(value)
    if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ConfigurationError("Unit paths cannot contain control characters")
    if re.fullmatch(r"[A-Za-z0-9_./:=@+,-]+", value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
    return f'"{escaped}"'


def unit_path(value):
    value = str(value)
    if any(ord(char) < 32 or ord(char) == 127 for char in value) or re.search(r"\s", value):
        raise ConfigurationError("Unit setting paths cannot contain whitespace or control characters")
    return value.replace("%", "%%")


def command(*words):
    return " ".join(unit_word(word) for word in words)


def render_units(paths, *, python, rclone):
    """Return the three user units, modelled on the validated trial services."""
    desktop = paths["desktop"]
    guard = (python, "-m", "cloudmarks", "mount-guard", "--settings", desktop)
    upload = f"""[Unit]
Description=CloudMarks upload folder (writable rclone mount)
Documentation=https://github.com/dzuritaa/cloudmarks

[Service]
Type=notify
ExecStartPre={command(*guard, "--view", "upload", "--operation", "prepare")}
ExecStart={
        command(
            rclone,
            "mount",
            "upload:",
            paths["upload_root"],
            "--config=" + paths["upload_config"],
            "--cache-dir=" + paths["upload_cache"],
            "--vfs-cache-mode=writes",
            "--vfs-cache-max-size=1G",
            f"--vfs-cache-min-free-space={paths['reserve_gib']}G",
            "--vfs-write-back=2s",
            "--dir-cache-time=2s",
            "--poll-interval=0",
            "--buffer-size=0",
            "--contimeout=5s",
            "--timeout=15s",
            "--umask=077",
            "--devname=cloudmarks-upload",
            "--log-level=ERROR",
            "--rc",
            "--rc-addr=unix://" + paths["upload_socket"],
        )
    }
ExecStop={command("fusermount3", "-u", paths["upload_root"])}
ExecStopPost={command(*guard, "--view", "upload", "--operation", "protect")}
UMask=0077
RuntimeDirectory=cloudmarks-upload
RuntimeDirectoryMode=0700
Restart=on-failure
RestartSec=10
SyslogIdentifier=cloudmarks-upload
KillMode=mixed
KillSignal=SIGINT
SendSIGKILL=no
TimeoutStartSec=45
TimeoutStopSec=30

[Install]
WantedBy=default.target
"""
    status = f"""[Unit]
Description=CloudMarks status helper
Documentation=https://github.com/dzuritaa/cloudmarks
Wants={UPLOAD_UNIT}
After={UPLOAD_UNIT}

[Service]
Type=simple
ExecStart={command(python, paths["helper"], "serve", "--settings", desktop)}
UMask=0077
RuntimeDirectory=cloudmarks
RuntimeDirectoryMode=0700
Restart=on-failure
RestartSec=3
TimeoutStopSec=5
NoNewPrivileges=true
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6

[Install]
WantedBy=default.target
"""
    view = f"""[Unit]
Description=CloudMarks offline view (read-only)
Documentation=https://github.com/dzuritaa/cloudmarks
Wants={STATUS_UNIT}
After={STATUS_UNIT}

[Service]
Type=simple
EnvironmentFile=-{unit_path(paths["mount_env"])}
ExecStartPre={command(*guard, "--view", "offline", "--operation", "prepare")}
ExecStart={command(python, "-m", "cloudmarks", "offline", "--config", paths["offline"], "mount")} $CLOUDMARKS_VIEW_FLAGS
ExecStopPost={command(*guard, "--view", "offline", "--operation", "protect")}
UMask=0077
Restart=on-failure
RestartSec=10
KillMode=mixed
KillSignal=SIGINT
SendSIGKILL=no
TimeoutStopSec=30

[Install]
WantedBy=default.target
"""
    return {UPLOAD_UNIT: upload, STATUS_UNIT: status, VIEW_UNIT: view}


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


def volume_of(path, run=subprocess.run):
    """Filesystem UUID and mount target of an existing directory."""
    result = run(
        ["findmnt", "--json", "--target", str(path), "--output", "UUID,TARGET,MAJ:MIN"],
        capture_output=True,
        timeout=5,
        check=True,
    )
    device = os.stat(path).st_dev
    rows = [
        row
        for row in json.loads(result.stdout)["filesystems"]
        if row.get("maj:min") == f"{os.major(device)}:{os.minor(device)}"
    ]
    if len(rows) != 1 or not rows[0].get("uuid"):
        raise StorageError(
            "The store folder must be on a disk with a filesystem UUID (not tmpfs or a network share)"
        )
    return rows[0]["uuid"], rows[0]["target"]


def init_store(config, values):
    """Equivalent of `cloudmarks offline init`: verify the account, then bind the store."""
    from .offline_config import initialize
    from .onedrive import OneDrive
    from .state import StateStore

    OneDrive(config, values).authenticate()
    initialize(config, values)
    with StateStore(config):
        pass


class Setup:
    def __init__(
        self,
        prompter,
        *,
        layout=None,
        run=subprocess.run,
        which=shutil.which,
        graph_factory=SetupGraph,
        volume=volume_of,
        store_initializer=init_store,
        python=sys.executable,
        say=print,
    ):
        self.prompt, self.run, self.which, self.say = prompter, run, which, say
        self.layout = layout or Layout.current()
        self.graph_factory, self.volume, self.store_initializer = graph_factory, volume, store_initializer
        self.python = python
        self.record = {"version": 1, "completed": [], "values": {}}

    # Progress record -----------------------------------------------------

    @property
    def values(self):
        return self.record["values"]

    def load_record(self):
        path = self.layout.file("setup.json")
        if path.exists():
            from .ipc import read_private_json

            value = read_private_json(path)
            if not isinstance(value, dict) or value.get("version") != 1:
                raise ConfigurationError("Setup progress record is not recognized")
            self.record = value
            return True
        return False

    def save(self, step):
        self.record["completed"].append(step)
        atomic_json(self.layout.file("setup.json"), self.record)

    def done(self, step):
        return step in self.record["completed"]

    # Steps -----------------------------------------------------------------

    def preflight(self):
        from .doctor import Report, system_checks

        report = Report()
        system_checks(report, run=self.run, which=self.which)
        for check in report.checks:
            self.say(f"  [{check['status'].upper():4}] {check['name']}: {check['detail']}")
        missing = [c["name"] for c in report.checks if c["status"] == "fail"]
        if missing:
            raise ConfigurationError("Install the missing requirements first: " + ", ".join(missing))
        if any(c["status"] == "warn" and c["name"].startswith("Nautilus") for c in report.checks):
            self.say("  Badges need the Nautilus Python packages; setup can continue without them.")

    def refuse_existing(self):
        config = self.layout.config
        existing = [config / "install.json", config / "desktop.json", self.layout.profile]
        existing += [self.layout.units / unit for unit in (UPLOAD_UNIT, STATUS_UNIT, VIEW_UNIT)]
        if any(path.exists() for path in existing):
            raise ConfigurationError(
                "CloudMarks is already set up for this user. Run `cloudmarks doctor`, or uninstall "
                "before starting a new setup"
            )

    def account(self):
        rclone = self.which("rclone")
        signin = self.layout.file("signin.conf")
        if not signin.exists():
            self.say(
                "\nSign in to OneDrive. rclone opens your browser; your password stays with Microsoft "
                "and the token is never shown.\nChoose the Microsoft account CloudMarks should use."
            )
            from .onedrive import clean_environment

            # Sign-in only: `rclone authorize` never lists or tests drives. Consumer
            # accounts can list stale extra drives that refuse access; setup takes
            # the account's own drive from Graph instead.
            try:
                result = self.run(
                    [rclone, "authorize", "onedrive", authorize_blob()],
                    stdout=subprocess.PIPE,
                    text=True,
                    env=clean_environment(),
                    timeout=900,
                )
            except subprocess.TimeoutExpired:
                raise StorageError("OneDrive sign-in timed out; run setup again to retry") from None
            token = authorized_token(result.stdout if result.returncode == 0 else "")
            write_origin(signin, {"type": "onedrive", "token": token})
        from .onedrive import private_config

        origin = dict(private_config(signin)["origin"])
        if origin.get("type") != "onedrive" or not origin.get("token"):
            raise ConfigurationError(
                "Sign-in did not produce a OneDrive credential; delete signin.conf and retry"
            )
        graph = self.graph_factory(rclone, signin)
        try:
            me = graph.get("me?$select=mail,userPrincipalName")
        except StorageError:
            signin.unlink()
            raise StorageError(
                "The sign-in cannot read the account's email (an expired sign-in, or one made by an "
                "older setup). Run setup again to sign in"
            ) from None
        drive = graph.get("me/drive?$select=id,driveType")
        email = me.get("mail") or me.get("userPrincipalName")
        if not isinstance(email, str) or not email:
            raise ScopeError("The signed-in account has no email address")
        if not isinstance(drive.get("id"), str) or drive.get("driveType") not in ("personal", "business"):
            raise ScopeError("The signed-in account has no usable OneDrive drive")
        drive_id = canonical_drive(drive["id"])
        if origin.get("drive_id") != drive_id or origin.get("drive_type") != drive["driveType"]:
            origin.update(drive_id=drive_id, drive_type=drive["driveType"])
            write_origin(signin, origin, replace=True)
        self.say(f"\nSigned in as {email}.")
        expected = self.prompt.answers.get("account_email")
        if expected is not None and str(expected).casefold() != email.casefold():
            raise ScopeError("The signed-in account does not match account_email in the answers file")
        if not self.prompt.confirm("confirm_account", "Use this account?", default=expected is not None):
            signin.unlink()
            raise ScopeError("Account not confirmed; run setup again to sign in with another account")
        self.values.update(rclone=rclone, email=email, drive=drive_id)

    def folder(self):
        graph = self.graph_factory(self.values["rclone"], self.layout.file("signin.conf"))
        if "folder" not in self.prompt.answers and self.prompt.interactive:
            listing = graph.get("me/drive/root/children?$select=id,name,folder,remoteItem&$top=200")
            names = sorted(
                item["name"]
                for item in listing.get("value", [])
                if "folder" in item and "remoteItem" not in item and isinstance(item.get("name"), str)
            )
            self.say("\nCloudMarks works inside one OneDrive folder. Top-level folders:")
            for index, name in enumerate(names, 1):
                self.say(f"  {index:3}. {name}")
            self.say(
                "Type a number, or a path such as Documents/CloudMarks. Create new folders on OneDrive first."
            )
        choice = self.prompt.value("folder", "Folder")
        if choice.isdigit() and "folder" not in self.prompt.answers:
            choice = names[int(choice) - 1] if 0 < int(choice) <= len(names) else ""
        path = choice.strip("/")
        if not path:
            raise ConfigurationError("Choose a folder inside OneDrive, not the whole drive")
        item = graph.get(
            "me/drive/root:/"
            + urllib.parse.quote(path, safe="/")
            + "?$select=id,name,folder,remoteItem,root,parentReference"
        )
        parent = item.get("parentReference") or {}
        if (
            "folder" not in item
            or "remoteItem" in item
            or "root" in item
            or canonical_drive(parent.get("driveId")) != self.values["drive"]
            or not isinstance(item.get("id"), str)
        ):
            raise ScopeError("Choose a regular folder in your own OneDrive (not a shared shortcut)")
        self.values.update(root=item["id"], folder_name=item.get("name", path))

    def storage(self):
        home = Path.home()
        default_parent = self.layout.data / "cloudmarks-offline"
        parent = Path(
            self.prompt.value("store_parent", "Folder for offline copies", str(default_parent), _absolute)
        )
        store = parent / "cloudmarks-store"
        if store.exists():
            raise StorageError("The chosen location already contains a CloudMarks store")
        make_directory(parent)
        uuid_value, target = self.volume(parent)
        space = os.statvfs(parent)
        free = space.f_bavail * space.f_frsize
        budget = self.prompt.value("budget_gib", "Maximum space for offline copies, GiB", 10, _positive)
        reserve = self.prompt.value(
            "reserve_gib", "Always keep this much disk space free, GiB", 20, _positive
        )
        if free < (budget + reserve) * GIB:
            self.say(f"  Note: about {free // GIB} GiB is free now; downloads stop at the reserve.")
        upload = Path(
            self.prompt.value("upload_root", "Upload folder", str(home / "CloudMarks/Upload"), _absolute)
        )
        offline = Path(
            self.prompt.value(
                "offline_root", "Offline view folder", str(home / "CloudMarks/Offline"), _absolute
            )
        )
        paths = (store, upload, offline, self.layout.config, self.layout.state)
        for a in paths:
            for b in paths:
                if a is not b and (a.is_relative_to(b) or b.is_relative_to(a)):
                    raise ConfigurationError("Store, views, settings and state must be separate folders")
        for view in (upload, offline):
            if view.exists() and (not view.is_dir() or any(view.iterdir())):
                raise StorageError("View folders must be new or empty")
        self.values.update(
            store=str(store),
            volume_uuid=uuid_value,
            volume_target=target,
            budget_gib=budget,
            reserve_gib=reserve,
            upload_root=str(upload),
            offline_root=str(offline),
            volume_id="cloudmarks-" + uuid.uuid4().hex,
        )

    def files(self):
        from .controls import binding
        from .ipc import validate_settings
        from .offline_config import load_offline
        from .onedrive import private_config

        v, layout = self.values, self.layout
        make_directory(layout.state)
        make_directory(layout.cache)
        origin = private_config(layout.file("signin.conf"))["origin"]
        if set(origin) - set(ORIGIN_KEYS):
            extra = set(origin) - set(ORIGIN_KEYS)
            if extra - {"region"} or origin.get("region", "global") != "global":
                raise ConfigurationError("Only global OneDrive accounts are supported")
        root_folder = v["drive"] + "#" + v["root"]
        credentials = {key: origin[key] for key in ORIGIN_KEYS if key in origin and key != "root_folder_id"}
        credentials["drive_id"] = v["drive"]
        credentials["root_folder_id"] = root_folder
        store, rclone_conf = Path(v["store"]), layout.file("transport.conf")
        transport = configparser.ConfigParser(interpolation=None)
        transport["origin"] = credentials
        transport["view"] = {
            "type": "union",
            "upstreams": f'"{store}/local:ro" origin::ro',
            "search_policy": "epall",
        }
        upload = configparser.ConfigParser(interpolation=None)
        upload["upload"] = credentials
        core = (
            "version = 1\n\n[account]\n"
            f"expected_drive_id = {json.dumps(v['drive'])}\n"
            f"allowed_root_id = {json.dumps(v['root'])}\n"
            "# Optional: other drives (such as a personal account) to refuse explicitly.\n"
            "blocked_drive_ids = []\n\n[storage]\n"
            f"root = {json.dumps(str(store))}\n"
            f"volume_id = {json.dumps(v['volume_id'])}\n"
            f"state_path = {json.dumps(str(layout.state / 'state.sqlite3'))}\n"
            "case_sensitive = false\n\n[status]\nmax_age_seconds = 300\n"
        )
        offline = {
            "version": 1,
            "core_config": str(layout.file("core.toml")),
            "rclone_config": str(rclone_conf),
            "rclone": v["rclone"],
            "visible_root": v["offline_root"],
            "expected_email": v["email"],
            "budget_bytes": v["budget_gib"] * GIB,
            "min_free_bytes": v["reserve_gib"] * GIB,
            "storage_mount_uuid": v["volume_uuid"],
            "storage_mount_target": v["volume_target"],
        }
        controls = {"version": 1, "view_service": VIEW_UNIT}
        if self.prompt.confirm(
            "live_updates",
            "Let Keep offline and Free local copy work while the offline view is open? (new, experimental)",
            default=False,
        ):
            controls["live_updates"] = True
        runtime = layout.runtime
        settings = {
            "version": 4,
            "mode": "onedrive",
            "enabled": False,
            "visible_root": v["offline_root"],
            "core_config": str(layout.file("offline.json")),
            "socket": str(runtime / "cloudmarks/status.sock"),
            "upload_root": v["upload_root"],
            "upload_config": str(layout.file("upload.conf")),
            "upload_socket": str(runtime / "cloudmarks-upload/rc.sock"),
            "control_config": str(layout.file("controls.json")),
        }
        written = {
            "core.toml": core.encode(),
            "transport.conf": _ini(transport),
            "upload.conf": _ini(upload),
            "offline.json": json_bytes(offline),
            "controls.json": json_bytes(controls),
            "mount.env": b"# Set to --offline for a no-network view of retained copies.\nCLOUDMARKS_VIEW_FLAGS=\n",
        }
        for name, contents in written.items():
            if not layout.file(name).exists():
                write_new(layout.file(name), contents)
        for view in (v["upload_root"], v["offline_root"]):
            Path(view).mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(view, 0o700)
        config, loaded = load_offline(layout.file("offline.json"))
        settings["control_binding"] = binding(config, settings, controls)
        source = layout.file("desktop-source.json")
        if not source.exists():
            write_new(source, json_bytes(validate_settings(settings)))
        layout.file("signin.conf").unlink(missing_ok=True)

    def store(self):
        from .offline_config import load_offline

        config, values = load_offline(self.layout.file("offline.json"))
        self.say("\nCreating the offline store and confirming the account with OneDrive...")
        self.store_initializer(config, values)

    def install(self):
        from .install import install

        install(self.layout.file("desktop-source.json"))

    def services(self):
        data = self.layout.data
        paths = {
            "desktop": str(self.layout.config / "desktop.json"),
            "helper": str(data / "cloudmarks/m2/run.py"),
            "offline": str(self.layout.file("offline.json")),
            "mount_env": str(self.layout.file("mount.env")),
            "upload_root": self.values["upload_root"],
            "upload_config": str(self.layout.file("upload.conf")),
            "upload_cache": str(self.layout.cache / "upload"),
            "upload_socket": str(self.layout.runtime / "cloudmarks-upload/rc.sock"),
            "reserve_gib": self.values["reserve_gib"],
        }
        make_directory(self.layout.units, mode=0o755)
        for name, text in render_units(paths, python=self.python, rclone=self.values["rclone"]).items():
            if not (self.layout.units / name).exists():
                write_new(self.layout.units / name, text.encode())
        if not self.which("systemctl"):
            self.say("systemctl is unavailable; start the three services manually.")
            return
        try:
            self.run(["systemctl", "--user", "daemon-reload"], timeout=30, check=True)
            if self.prompt.confirm(
                "enable_services", "Start CloudMarks now and at every login?", default=True
            ):
                self.run(
                    ["systemctl", "--user", "enable", "--now", UPLOAD_UNIT, STATUS_UNIT, VIEW_UNIT],
                    timeout=180,
                    check=True,
                )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            raise StorageError(
                "A CloudMarks service did not start. Check `systemctl --user status cloudmarks-upload.service` "
                "and `cloudmarks doctor`, then run setup again to finish"
            ) from None
        if self.prompt.confirm("enable_badges", "Show CloudMarks badges in Files?", default=True):
            from .install import set_enabled

            set_enabled(True)

    # Driver ----------------------------------------------------------------

    def plan(self):
        layout = self.layout
        lines = [
            "Planned setup (nothing has been changed):",
            f"  Private profile:   {layout.profile}",
            f"  State database:    {layout.state / 'state.sqlite3'}",
            f"  Offline store:     {self.prompt.answers.get('store_parent', layout.data / 'cloudmarks-offline')}"
            "/cloudmarks-store",
            f"  Upload folder:     {self.prompt.answers.get('upload_root', Path.home() / 'CloudMarks/Upload')}",
            f"  Offline view:      {self.prompt.answers.get('offline_root', Path.home() / 'CloudMarks/Offline')}",
            f"  Installed adapter: {layout.data / 'cloudmarks/m2'}",
            f"  User services:     {', '.join((UPLOAD_UNIT, STATUS_UNIT, VIEW_UNIT))} in {layout.units}",
            "Steps: preflight, sign in with rclone, choose a OneDrive folder, choose storage, write "
            "private settings, create the store, install the Files adapter, create services.",
        ]
        return "\n".join(lines)

    def execute(self):
        resumed = self.load_record()
        if not resumed:
            self.refuse_existing()
            self.say("CloudMarks setup\n\nChecking requirements:")
            self.preflight()
            self.say(
                "\nCloudMarks is experimental. Start with a test account or a folder of disposable files; "
                "keep backups of anything important."
            )
            if not self.prompt.confirm("accept_experimental", "Continue?", default=False):
                raise ConfigurationError("Setup cancelled; nothing was changed")
            make_directory(self.layout.config)
            make_directory(self.layout.profile)
            atomic_json(self.layout.file("setup.json"), self.record)
        else:
            self.say("Resuming CloudMarks setup.")
        for step in STEPS:
            if not self.done(step):
                getattr(self, step)()
                self.save(step)
        self.say("\nSetup complete. Checking the result:\n")
        from .doctor import run_doctor

        report = run_doctor(run=self.run, which=self.which)
        self.say(report.render())
        self.say(
            "\nOpen your upload and offline folders in Files. Right-click a file or folder to keep it "
            "offline. A new Files window may be needed before badges appear."
        )
        return report


def _absolute(value):
    path = Path(os.path.expanduser(str(value)))
    if not path.is_absolute() or ".." in path.parts or path == Path("/"):
        raise ConfigurationError("Use an absolute folder path")
    return str(path)


def _positive(value):
    number = int(value)
    if number < 1:
        raise ValueError("Use a whole number of at least 1")
    return number


def _ini(parser):
    from io import StringIO

    stream = StringIO()
    parser.write(stream)
    return stream.getvalue().encode()


def load_answers(path):
    if path is None:
        return {}
    with open(path, "rb") as stream:
        value = tomllib.load(stream)
    allowed = {
        "accept_experimental",
        "account_email",
        "confirm_account",
        "folder",
        "store_parent",
        "budget_gib",
        "reserve_gib",
        "upload_root",
        "offline_root",
        "enable_services",
        "enable_badges",
        "live_updates",
    }
    if set(value) - allowed:
        raise ConfigurationError("Unknown setup answers: " + ", ".join(sorted(set(value) - allowed)))
    return value


def main(args):
    answers = load_answers(args.answers)
    interactive = sys.stdin.isatty() and args.answers is None
    setup = Setup(Prompter(answers, interactive=interactive))
    if args.dry_run:
        print(setup.plan())
        return
    setup.execute()
