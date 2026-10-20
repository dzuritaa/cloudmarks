"""Release gate rejects missing code/assets, unsafe content and license drift."""

import io
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts.check_dist import check
from scripts.compare_wheels import payload


class DistributionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cm-release-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.dist = self.root / "dist"
        self.dist.mkdir()
        self.files = {
            "pyproject.toml": b'[project]\nversion="0.1.0a1"\nrequires-python=">=3.11"\nlicense="MIT"\n',
            "cloudmarks/__init__.py": b'__version__ = "0.1.0a1"\n',
            "cloudmarks/icons/example.svg": b"<svg/>",
            "LICENSE": b"Artificial license fixture\n",
            "MANIFEST.in": b"",
            "README.md": b"",
            "CHANGELOG.md": b"",
            "CONTRIBUTING.md": b"",
            "SECURITY.md": b"",
            "tests/test_example.py": b"# fixture\n",
            "examples/cloudmarks-status.service": b"# fixture\n",
            "scripts/example.py": b"# fixture\n",
            ".github/workflows/ci.yml": b"# fixture\n",
            "docs/result.md": b"# fixture\n",
        }
        for name, data in self.files.items():
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.metadata = (
            b"Name: cloudmarks\nVersion: 0.1.0a1\nRequires-Python: >=3.11\nLicense-Expression: MIT\n"
        )
        self.wheel = self.dist / "cloudmarks-0.1.0a1-py3-none-any.whl"
        self.sdist = self.dist / "cloudmarks-0.1.0a1.tar.gz"

    def archives(self, *, wheel_changes=None, source_changes=None):
        wheel_files = {name: data for name, data in self.files.items() if name.startswith("cloudmarks/")}
        wheel_files.update(
            {
                "cloudmarks-0.1.0a1.dist-info/METADATA": self.metadata,
                "cloudmarks-0.1.0a1.dist-info/licenses/LICENSE": self.files["LICENSE"],
            }
        )
        for name, value in (wheel_changes or {}).items():
            if value is None:
                wheel_files.pop(name)
            else:
                wheel_files[name] = value
        with zipfile.ZipFile(self.wheel, "w") as archive:
            for name, data in wheel_files.items():
                archive.writestr(name, data)
        source_files = dict(self.files)
        source_files.update(source_changes or {})
        with tarfile.open(self.sdist, "w:gz") as archive:
            for name, data in source_files.items():
                if data is None:
                    continue
                info = tarfile.TarInfo("cloudmarks-0.1.0a1/" + name)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))

    def test_release_contains_matching_package_license_and_checksums(self):
        self.archives()
        sums = check(self.dist, self.source, release=True)
        self.assertEqual(len(sums), 2)
        self.assertIn(self.wheel.name, (self.dist / "SHA256SUMS").read_text())

    def test_missing_or_changed_runtime_asset_is_rejected(self):
        for value in (None, b"different"):
            self.archives(wheel_changes={"cloudmarks/icons/example.svg": value})
            with self.assertRaises(ValueError):
                check(self.dist, self.source)

    def test_unselected_or_drifting_license_blocks_release(self):
        self.archives()
        (self.source / "pyproject.toml").write_text(
            '[project]\nversion="0.1.0a1"\nrequires-python=">=3.11"\n'
        )
        with self.assertRaisesRegex(ValueError, "choose a license"):
            check(self.dist, self.source, release=True)
        (self.source / "pyproject.toml").write_bytes(self.files["pyproject.toml"])
        self.archives(wheel_changes={"cloudmarks-0.1.0a1.dist-info/licenses/LICENSE": b"Other terms"})
        with self.assertRaisesRegex(ValueError, "license mismatch"):
            check(self.dist, self.source, release=True)

    def test_private_or_escaping_archive_members_are_rejected(self):
        for name in (
            "../escape",
            "secrets/token.json",
            "cloudmarks/state.sqlite3",
            "cloudmarks/__pycache__/file.pyc",
        ):
            self.archives(source_changes={name: b"artificial"})
            with self.assertRaises(ValueError):
                check(self.dist, self.source)

    def test_missing_reproduction_files_or_version_mismatch_is_rejected(self):
        self.archives(source_changes={"tests/test_example.py": None})
        with self.assertRaises(KeyError):
            check(self.dist, self.source)
        self.archives()
        (self.source / "cloudmarks/__init__.py").write_text('__version__ = "0.2.0"\n')
        with self.assertRaises(ValueError):
            check(self.dist, self.source)

    def test_wheel_comparison_detects_rebuilt_payload_difference(self):
        self.archives()
        first = payload(self.wheel)
        self.archives(wheel_changes={"cloudmarks/icons/example.svg": b"changed"})
        self.assertNotEqual(first, payload(self.wheel))
