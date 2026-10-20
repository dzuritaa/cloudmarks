"""User-only deployment with an ownership manifest and conservative removal."""

import hashlib
import json
import os
import re
import stat
from pathlib import Path

from .ipc import load_settings, private_directory, read_private_json, validate_settings
from .model import ConfigurationError
from .paths import open_absolute_directory


def locations():
    data = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    config = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    for path in (data, config):
        if not path.is_absolute() or ".." in path.parts:
            raise ConfigurationError("XDG directories must be absolute without parent traversal")
    return data, config / "cloudmarks"


def make_directory(path, mode=0o700):
    """Create missing user directories, refusing symlink traversal."""
    path = Path(path)
    missing = []
    cursor = path
    while not cursor.exists():
        missing.append(cursor)
        cursor = cursor.parent
    fd = open_absolute_directory(cursor)
    os.close(fd)
    for directory in reversed(missing):
        directory.mkdir(mode=mode)
    fd = open_absolute_directory(path)
    info = os.fstat(fd)
    os.close(fd)
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022:
        raise ConfigurationError("Deployment directory must be user-owned and not writable by others")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def write_new(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        Path(path).unlink(missing_ok=True)
        raise


def json_bytes(value):
    return (json.dumps(value, indent=2, ensure_ascii=True) + "\n").encode()


def targets(data, config):
    return (
        data / "cloudmarks/m2",
        data / "nautilus-python/extensions/cloudmarks_nautilus.py",
        config / "desktop.json",
    )


def install(source_settings):
    settings = dict(load_settings(source_settings), enabled=False)
    data, config = locations()
    root, extension, desktop = targets(data, config)
    make_directory(config)
    fd = private_directory(config)
    os.close(fd)
    manifest = config / "install.json"
    if manifest.exists() or root.exists() or extension.exists() or desktop.exists():
        raise ConfigurationError("CloudMarks deployment already exists; uninstall it before reinstalling")
    make_directory(root.parent)
    make_directory(extension.parent)
    package = Path(__file__).parent
    files = {
        root / "cloudmarks" / path.relative_to(package): path.read_bytes()
        for path in package.rglob("*")
        if path.is_file() and path.suffix in (".py", ".svg", ".theme") and "__pycache__" not in path.parts
    }
    prefix = f"import sys\nsys.path.insert(0, {str(root)!r})\n"
    files[extension] = (prefix + "from cloudmarks.nautilus_extension import CloudMarksExtension\n").encode()
    files[root / "run.py"] = (prefix + "from cloudmarks.__main__ import main\nmain()\n").encode()
    files[desktop] = json_bytes(settings)
    written = []
    try:
        # Install disabled; no desktop restart or service enablement occurs.
        for path, contents in files.items():
            make_directory(path.parent)
            write_new(path, contents)
            written.append(path)
        write_new(
            manifest, json_bytes({"version": 1, "files": {str(p): digest(c) for p, c in files.items()}})
        )
    except BaseException:
        for path in reversed(written):
            path.unlink()
        remove_empty_tree(root)
        raise
    return desktop


def inventory():
    data, config = locations()
    root, extension, desktop = targets(data, config)
    value = read_private_json(config / "install.json")
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "files"}
        or value["version"] != 1
        or not isinstance(value["files"], dict)
        or not 1 <= len(value["files"]) <= 128
    ):
        raise ConfigurationError("Invalid installation manifest")
    for name, checksum in value["files"].items():
        path = Path(name)
        if (
            not isinstance(checksum, str)
            or len(checksum) != 64
            or not path.is_absolute()
            or ".." in path.parts
            or not (path.is_relative_to(root) or path in (extension, desktop))
        ):
            raise ConfigurationError("Installation manifest escapes its deployment")
    return value, config, root, extension, desktop


def checked_contents(path, checksum, *, mutable_settings=False):
    parent = open_absolute_directory(path.parent)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_uid != os.getuid()
                or info.st_size > 1024 * 1024
            ):
                raise ConfigurationError("Installed file ownership/type changed; preserving it")
            contents = stream.read(1024 * 1024 + 1)
        canonical = (
            json_bytes(dict(validate_settings(json.loads(contents)), enabled=False))
            if mutable_settings
            else contents
        )
        if digest(canonical) != checksum:
            raise ConfigurationError("Installed file was modified; preserving the deployment for review")
        return contents
    finally:
        os.close(parent)


def atomic_json(path, value):
    # Private directory; never follow an existing target symlink.
    temporary = path.with_name(path.name + ".new")
    write_new(temporary, json_bytes(value))
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def set_enabled(enabled):
    value, _, _, _, desktop = inventory()
    contents = checked_contents(desktop, value["files"][str(desktop)], mutable_settings=True)
    settings = validate_settings(json.loads(contents))
    settings["enabled"] = enabled
    atomic_json(desktop, settings)


def remove_empty_tree(root):
    for directory in sorted(
        (p for p in root.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True
    ):
        try:
            directory.rmdir()
        except OSError:
            pass
    try:
        root.rmdir()
    except OSError:
        pass


def uninstall():
    value, config, root, _, desktop = inventory()
    # Preflight everything before removing anything. Leave edited/unrelated
    # files untouched, and never remove the offline store or fixture directory.
    for name, checksum in value["files"].items():
        checked_contents(Path(name), checksum, mutable_settings=Path(name) == desktop)
    caches = []
    for name in value["files"]:
        source = Path(name)
        if source.suffix != ".py" or not source.is_relative_to(root):
            continue
        directory = source.parent / "__pycache__"
        if not directory.exists():
            continue
        fd = open_absolute_directory(directory)
        try:
            for entry in os.listdir(fd):
                if re.fullmatch(re.escape(source.stem) + r"\.cpython-\d+(?:\.opt-\d+)?\.pyc", entry):
                    info = os.stat(entry, dir_fd=fd, follow_symlinks=False)
                    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                        raise ConfigurationError(
                            "Generated bytecode ownership changed; preserving deployment"
                        )
                    caches.append(directory / entry)
        finally:
            os.close(fd)
    for path in caches:
        path.unlink()
    for name in value["files"]:
        Path(name).unlink()
    (config / "install.json").unlink()
    remove_empty_tree(root)
    for directory in (root, root.parent, config):
        try:
            directory.rmdir()
        except OSError:
            pass
