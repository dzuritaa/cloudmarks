"""Small, explicit state/evidence types; no filesystem or network work."""

import math
import re
from dataclasses import dataclass
from enum import StrEnum


class CloudMarksError(Exception):
    """Base for expected, fail-closed errors."""


class ConfigurationError(CloudMarksError):
    pass


class ScopeError(CloudMarksError):
    pass


class StorageError(CloudMarksError):
    pass


class IntegrityError(CloudMarksError):
    pass


def text(value, name):
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 1024:
        raise ConfigurationError(f"{name} must be nonempty, bounded text without surrounding whitespace")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ConfigurationError(f"{name} contains a control character")
    return value


def canonical_drive(value):
    value = text(value, "drive ID")
    # Personal drive IDs are hexadecimal. Business IDs are opaque/base64-like
    # strings: applying casefold to those would conflate distinct identities.
    return value.upper() if re.fullmatch(r"[a-fA-F0-9]{16}", value) else value


@dataclass(frozen=True)
class FileIdentity:
    drive_id: str
    item_id: str

    def __post_init__(self):
        object.__setattr__(self, "drive_id", canonical_drive(self.drive_id))
        text(self.item_id, "item ID")


@dataclass(frozen=True)
class RemoteFile:
    identity: FileIdentity
    root_id: str
    relative_path: str
    revision: str
    size: int
    observed_at: float
    sha256: str | None = None
    deleted: bool = False
    is_directory: bool = False

    def __post_init__(self):
        if not isinstance(self.identity, FileIdentity):
            raise ConfigurationError("Remote file must have a stable file identity")
        text(self.root_id, "root ID")
        text(self.revision, "revision")
        if isinstance(self.size, bool) or not isinstance(self.size, int) or self.size < 0:
            raise ConfigurationError("File size must be a nonnegative integer")
        if (
            isinstance(self.observed_at, bool)
            or not isinstance(self.observed_at, (float, int))
            or not math.isfinite(self.observed_at)
        ):
            raise ConfigurationError("Observation time must be finite")
        if self.sha256 is not None and not re.fullmatch(r"[0-9a-f]{64}", self.sha256):
            raise ConfigurationError("Expected SHA-256 must be a lowercase hexadecimal digest")
        if not isinstance(self.deleted, bool) or not isinstance(self.is_directory, bool):
            raise ConfigurationError("Deleted and directory flags must be booleans")


@dataclass(frozen=True)
class LocalCopy:
    identity: FileIdentity
    revision: str
    object_path: str
    size: int
    sha256: str

    def __post_init__(self):
        if not isinstance(self.identity, FileIdentity):
            raise ConfigurationError("Local copy must have a stable identity")
        text(self.revision, "local revision")
        if not re.fullmatch(r"objects/[0-9a-f]{32}\.blob", self.object_path):
            raise ConfigurationError("Local copy must reference a published immutable object")
        if isinstance(self.size, bool) or not isinstance(self.size, int) or self.size < 0:
            raise ConfigurationError("Local copy size must be nonnegative")
        if not re.fullmatch(r"[0-9a-f]{64}", self.sha256):
            raise ConfigurationError("Local copy checksum is invalid")


class State(StrEnum):
    CLOUD_ONLY = "cloud_only"
    SAVED_LOCALLY = "saved_locally"
    KEPT_OFFLINE = "kept_offline"
    TRANSFERRING = "transferring"
    ERROR = "error"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class FileStatus:
    state: State
    reason: str

    @property
    def green(self):
        return self.state in (State.SAVED_LOCALLY, State.KEPT_OFFLINE)
