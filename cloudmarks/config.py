"""Versioned TOML configuration and account/root containment."""

import math
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .model import ConfigurationError, RemoteFile, ScopeError, canonical_drive, text
from .paths import relative_parts


def absolute_path(value, name):
    text(value, name)
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or path == Path("/"):
        raise ConfigurationError(f"{name} must be an absolute non-root path without parent traversal")
    return path


def integer(value, name, minimum):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ConfigurationError(f"{name} must be an integer >= {minimum}")
    return value


@dataclass(frozen=True)
class Config:
    expected_drive_id: str
    allowed_root_id: str
    blocked_drive_ids: tuple[str, ...]
    storage_root: Path
    volume_id: str
    state_path: Path
    max_age_seconds: float = 300
    case_sensitive: bool = False

    def __post_init__(self):
        expected = canonical_drive(self.expected_drive_id)
        # Blocking other known drives is an optional extra guard; the expected
        # drive and allowed root are always enforced.
        if not isinstance(self.blocked_drive_ids, tuple):
            raise ConfigurationError("Blocked drive IDs must be a list")
        blocked = tuple(canonical_drive(item) for item in self.blocked_drive_ids)
        if expected in blocked:
            raise ConfigurationError("Expected drive is blocked")
        object.__setattr__(self, "expected_drive_id", expected)
        object.__setattr__(self, "blocked_drive_ids", blocked)
        text(self.allowed_root_id, "allowed root ID")
        text(self.volume_id, "volume ID")
        object.__setattr__(self, "storage_root", absolute_path(str(self.storage_root), "storage root"))
        object.__setattr__(self, "state_path", absolute_path(str(self.state_path), "state path"))
        if (
            isinstance(self.max_age_seconds, bool)
            or not isinstance(self.max_age_seconds, (int, float))
            or not math.isfinite(self.max_age_seconds)
            or self.max_age_seconds <= 0
        ):
            raise ConfigurationError("max_age_seconds must be finite and positive")
        if not isinstance(self.case_sensitive, bool):
            raise ConfigurationError("case_sensitive must be a boolean")

    def validate_identity(self, identity):
        if identity.drive_id in self.blocked_drive_ids or identity.drive_id != self.expected_drive_id:
            raise ScopeError("File is not in the expected test drive")

    def validate_remote(self, remote: RemoteFile):
        self.validate_identity(remote.identity)
        if remote.root_id != self.allowed_root_id:
            raise ScopeError("File is not in the allowed test root")
        relative_parts(remote.relative_path)

    def path_key(self, relative_path):
        relative_parts(relative_path)
        return relative_path if self.case_sensitive else relative_path.casefold()


def load_config(path):
    try:
        with open(path, "rb") as stream:
            value = tomllib.load(stream)
        if (
            set(value) != {"version", "account", "storage", "status"}
            or integer(value["version"], "version", 1) != 1
        ):
            raise ConfigurationError("Expected configuration version 1 and the documented sections")
        account, storage, status = (value[name] for name in ("account", "storage", "status"))
        if (
            not {"expected_drive_id", "allowed_root_id"}
            <= set(account)
            <= {
                "expected_drive_id",
                "allowed_root_id",
                "blocked_drive_ids",
            }
        ):
            raise ConfigurationError("Unexpected or missing account configuration")
        if set(storage) != {"root", "volume_id", "state_path", "case_sensitive"}:
            raise ConfigurationError("Unexpected or missing storage configuration")
        if set(status) != {"max_age_seconds"}:
            raise ConfigurationError("Unexpected or missing status configuration")
        blocked = account.get("blocked_drive_ids", [])
        if not isinstance(blocked, list):
            raise ConfigurationError("blocked_drive_ids must be a list")
        return Config(
            account["expected_drive_id"],
            account["allowed_root_id"],
            tuple(blocked),
            absolute_path(storage["root"], "storage root"),
            storage["volume_id"],
            absolute_path(storage["state_path"], "state path"),
            status["max_age_seconds"],
            storage["case_sensitive"],
        )
    except (OSError, tomllib.TOMLDecodeError, KeyError, TypeError, ValueError) as error:
        raise ConfigurationError("Cannot read a valid CloudMarks configuration") from error
