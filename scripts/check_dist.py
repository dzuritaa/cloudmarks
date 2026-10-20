#!/usr/bin/env python3
"""Check release contents against source without extracting untrusted archives."""

import argparse
import hashlib
import json
import tarfile
import tomllib
import zipfile
from email.parser import BytesParser
from pathlib import Path, PurePosixPath


def safe_name(name):
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name:
        raise ValueError("Archive contains an unsafe path")
    if any(part in ("__pycache__", ".env", "credentials", "secrets", ".git") for part in path.parts):
        raise ValueError("Archive contains private/generated content")
    if name.endswith((".pyc", ".pyo", ".sqlite3", ".lock", ".log")):
        raise ValueError("Archive contains private/generated content")


def check(directory, source, *, release=False):
    project = tomllib.loads((source / "pyproject.toml").read_text())["project"]
    version = project["version"]
    wheel = directory / f"cloudmarks-{version}-py3-none-any.whl"
    sdist = directory / f"cloudmarks-{version}.tar.gz"
    if set(directory.glob("*.whl")) != {wheel} or set(directory.glob("*.tar.gz")) != {sdist}:
        raise ValueError("Expected exactly the current version's wheel and sdist")
    license_id = project.get("license")
    if release and (not license_id or not (source / "LICENSE").is_file()):
        raise ValueError("Release blocked: choose a license and include LICENSE before distributing")
    expected = {
        str(path.relative_to(source)): path.read_bytes()
        for path in (source / "cloudmarks").rglob("*")
        if path.is_file() and path.suffix in (".py", ".svg", ".theme") and "__pycache__" not in path.parts
    }
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError("Duplicate wheel members")
        for info in archive.infolist():
            safe_name(info.filename)
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError("Wheel contains a link")
            if info.filename.startswith("cloudmarks/"):
                if info.filename not in expected or archive.read(info) != expected[info.filename]:
                    raise ValueError("Wheel package does not match source")
            elif not info.filename.startswith(f"cloudmarks-{version}.dist-info/"):
                raise ValueError("Unexpected wheel content")
        if set(name for name in names if name.startswith("cloudmarks/")) != set(expected):
            raise ValueError("Wheel is missing package files")
        metadata = BytesParser().parsebytes(archive.read(f"cloudmarks-{version}.dist-info/METADATA"))
        if (metadata["Name"], metadata["Version"], metadata["Requires-Python"]) != (
            "cloudmarks",
            version,
            project["requires-python"],
        ):
            raise ValueError("Wheel metadata mismatch")
        if license_id and (
            metadata["License-Expression"] != license_id
            or archive.read(f"cloudmarks-{version}.dist-info/licenses/LICENSE")
            != (source / "LICENSE").read_bytes()
        ):
            raise ValueError("Wheel license mismatch")
        if (source / "cloudmarks/__init__.py").read_text().split('__version__ = "')[1].split('"')[
            0
        ] != version:
            raise ValueError("CLI/package version disagrees with project version")
    with tarfile.open(sdist) as archive:
        members = archive.getmembers()
        names = [value.name for value in members]
        if len(names) != len(set(names)):
            raise ValueError("Duplicate source archive members")
        prefix = f"cloudmarks-{version}/"
        for member in members:
            safe_name(member.name)
            if not (member.isfile() or member.isdir()) or not (member.name + "/").startswith(prefix):
                raise ValueError("Unexpected source archive member")
        required = {
            "pyproject.toml",
            "MANIFEST.in",
            "README.md",
            "CHANGELOG.md",
            "CONTRIBUTING.md",
            "SECURITY.md",
        }
        required.update(expected)
        for top in ("tests", "examples", "scripts", ".github/workflows", "docs"):
            required.update(
                str(p.relative_to(source))
                for p in (source / top).rglob("*")
                if p.is_file() and p.suffix in (".py", ".md", ".json", ".yml", ".toml", ".conf", ".service")
            )
        if license_id:
            required.add("LICENSE")
        for name in required:
            member = archive.extractfile(prefix + name)
            if member is None or member.read() != (source / name).read_bytes():
                raise ValueError("Source archive is missing or differs from source: " + name)
    sums = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in (wheel, sdist)}
    (directory / "SHA256SUMS").write_text("".join(f"{sha}  {name}\n" for name, sha in sorted(sums.items())))
    return sums


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--release", action="store_true")
    args = parser.parse_args()
    try:
        sums = check(args.directory, args.source, release=args.release)
        project = tomllib.loads((args.source / "pyproject.toml").read_text())["project"]
        print(
            json.dumps(
                {
                    "version": project["version"],
                    "license": project.get("license", "UNSELECTED; release blocked"),
                    "sha256": sums,
                }
            )
        )
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, tarfile.TarError) as error:
        parser.exit(1, f"Package validation failed: {error}\n")


if __name__ == "__main__":
    main()
