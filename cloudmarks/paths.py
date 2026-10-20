"""Linux directory-FD operations; never follow a stored path out of its root."""

import hashlib
import os
import stat
import uuid
from contextlib import contextmanager
from pathlib import Path

from .model import IntegrityError, ScopeError, StorageError

DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
READ_FILE = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
MARKER = ".cloudmarks-volume"


def relative_parts(value):
    if not isinstance(value, str) or not value or len(value) > 4096 or value.startswith("/"):
        raise ScopeError("Expected a bounded relative file path")
    if "\\" in value or ":" in value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ScopeError("Unsupported or unsafe path character")
    parts = value.split("/")
    try:
        if any(part in ("", ".", "..") or len(part.encode("utf-8")) > 255 for part in parts):
            raise ScopeError("Path has an empty, oversized, or traversing component")
    except UnicodeError:
        raise ScopeError("Path is not valid Unicode") from None
    return tuple(parts)


def open_absolute_directory(path):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise ScopeError("Directory must be absolute and cannot traverse parents")
    fd = os.open("/", DIRECTORY)
    try:
        for part in path.parts[1:]:
            next_fd = os.open(part, DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def fingerprint(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_nlink)


class SecureRoot:
    def __init__(self, path, volume_id):
        self.path = Path(path)
        self.fd = None
        try:
            self.fd = open_absolute_directory(self.path)
            self.device = os.fstat(self.fd).st_dev
            with self.open_regular(MARKER) as marker:
                value = os.read(marker, 1025)
            if len(value) > 1024 or value.decode("utf-8").rstrip("\n") != volume_id:
                raise StorageError("Offline disk marker does not match")
        except (OSError, UnicodeError, ScopeError, StorageError):
            self.close()
            raise StorageError("Offline storage is unavailable or does not match its marker") from None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    @contextmanager
    def parent(self, relative_path, create=False):
        parts = relative_parts(relative_path)
        fd = os.dup(self.fd)
        try:
            for part in parts[:-1]:
                if create:
                    try:
                        os.mkdir(part, 0o700, dir_fd=fd)
                    except FileExistsError:
                        pass
                next_fd = os.open(part, DIRECTORY, dir_fd=fd)
                if os.fstat(next_fd).st_dev != self.device:
                    os.close(next_fd)
                    raise ScopeError("Nested filesystem is outside the storage root")
                os.close(fd)
                fd = next_fd
            yield fd, parts[-1]
        finally:
            os.close(fd)

    @contextmanager
    def open_regular(self, relative_path):
        with self.parent(relative_path) as (parent, name):
            fd = os.open(name, READ_FILE, dir_fd=parent)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_dev != self.device:
                raise ScopeError("Offline content must be a regular file with one link on its expected disk")
            yield fd
        finally:
            os.close(fd)

    def verify(self, relative_path, size, sha256):
        with self.open_regular(relative_path) as fd:
            before = os.fstat(fd)
            if before.st_size != size:
                raise IntegrityError("Local file length does not match the completed copy")
            value = hashlib.sha256()
            count = 0
            while block := os.read(fd, 1024 * 1024):
                count += len(block)
                value.update(block)
            if fingerprint(before) != fingerprint(os.fstat(fd)):
                raise IntegrityError("Local file changed during verification")
            if count != size or value.hexdigest() != sha256:
                raise IntegrityError("Local file is partial, changed, or corrupt")
            return fingerprint(before)

    def current_fingerprint(self, relative_path):
        with self.open_regular(relative_path) as fd:
            return fingerprint(os.fstat(fd))

    def revalidate(self, volume_id):
        with SecureRoot(self.path, volume_id) as current:
            before, after = os.fstat(self.fd), os.fstat(current.fd)
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise StorageError("Offline disk changed during verification")

    def stage(self, chunks, expected_size, expected_sha256):
        relative = "objects/.partial-" + uuid.uuid4().hex
        with self.parent(relative, create=True) as (parent, name):
            fd = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=parent,
            )
            try:
                count = 0
                value = hashlib.sha256()
                for block in chunks:
                    if not isinstance(block, bytes):
                        raise IntegrityError("Transport yielded non-byte content")
                    count += len(block)
                    if count > expected_size:
                        raise IntegrityError("Download exceeded the expected byte count")
                    value.update(block)
                    remaining = memoryview(block)
                    while remaining:
                        written = os.write(fd, remaining)
                        if written <= 0:
                            raise OSError("Download write made no progress")
                        remaining = remaining[written:]
                if count != expected_size or value.hexdigest() != expected_sha256:
                    raise IntegrityError("Download is partial or its checksum is wrong")
                os.fsync(fd)
            finally:
                os.close(fd)
        # Retain partial files on failure as unreferenced recovery candidates.
        return relative

    def publish(self, candidate):
        if not candidate.startswith("objects/.partial-") or len(relative_parts(candidate)) != 2:
            raise ScopeError("Only a staged candidate may be published")
        target = "objects/" + uuid.uuid4().hex + ".blob"
        with self.parent(candidate) as (parent, name):
            # link + unlink has no overwrite behavior, unlike os.replace. The
            # resulting published object has one link; old versions stay intact.
            os.link(name, target.split("/")[1], src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
            os.unlink(name, dir_fd=parent)
            os.fsync(parent)
        return target
