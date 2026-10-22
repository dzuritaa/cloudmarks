"""Linux cache change notifications: scan once, then inspect changed files only."""

import ctypes
import os
import stat
import struct

from .limits import CACHE_ENTRIES
from .model import ScopeError, StorageError
from .paths import open_absolute_directory, relative_parts

# Modify, attrib, close-write, moves, create, delete and directory self events.
MASK = 0x00000FCE
OVERFLOW = 0x00004000
ISDIR = 0x40000000
IGNORED = 0x00008000


class Rescan(StorageError):
    pass


class CacheWatch:
    def __init__(self, data, meta):
        self.fd = -1
        self.watches = {}
        libc = ctypes.CDLL(None, use_errno=True)
        self.fd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if self.fd < 0:
            raise StorageError("Cache change notifications are unavailable")
        try:
            for path in (data, meta):
                root = open_absolute_directory(path)
                try:
                    for directory, dirs, _files, fd in os.fwalk(".", dir_fd=root, follow_symlinks=False):
                        if len(self.watches) >= CACHE_ENTRIES or len(directory.split("/")) > 65:
                            raise StorageError("Cache watch exceeds its bound")
                        for name in dirs:
                            if stat.S_ISLNK(os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode):
                                raise ScopeError("Cache contains a directory link")
                        # The descriptor pins the directory; no path-link races.
                        wd = libc.inotify_add_watch(self.fd, f"/proc/self/fd/{fd}".encode(), MASK)
                        if wd < 0:
                            raise StorageError("Cache directory watch could not be established")
                        self.watches[wd] = "" if directory == "." else directory[2:]
                finally:
                    os.close(root)
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def changed(self):
        paths = set()
        while True:
            try:
                data = os.read(self.fd, 256 * 1024)
            except BlockingIOError:
                return paths
            offset = 0
            while offset < len(data):
                wd, mask, cookie, length = struct.unpack_from("iIII", data, offset)
                raw = data[offset + 16 : offset + 16 + length].split(b"\0", 1)[0]
                offset += 16 + length
                if mask & (OVERFLOW | IGNORED | ISDIR) or wd not in self.watches or not raw:
                    raise Rescan("Cache directory changed; rebuild its bounded watch")
                name = os.fsdecode(raw)
                parent = self.watches[wd]
                path = parent + "/" + name if parent else name
                relative_parts(path)
                paths.add(path)
                if len(paths) > CACHE_ENTRIES:
                    raise Rescan("Cache notifications exceeded their bound")
