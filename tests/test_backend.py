"""Provider contracts must match the OneDrive implementation and the callers' fakes."""

import inspect
import unittest
from unittest.mock import patch

from cloudmarks import backend
from cloudmarks.onedrive import OneDrive
from tests.test_offline import Backend as OfflineFake
from tests.test_tracking import Catalog

CONTRACTS = (
    backend.Metadata,
    backend.Transfer,
    backend.OfflineBackend,
    backend.ActivityBackend,
    backend.TransportProof,
)


def methods(protocol):
    return {
        name: value
        for klass in protocol.__mro__
        if klass not in (object, backend.Protocol) and getattr(klass, "_is_protocol", False)
        for name, value in vars(klass).items()
        if callable(value) and not name.startswith("_")
    }


class ContractTests(unittest.TestCase):
    def test_onedrive_implements_every_contract_with_compatible_signatures(self):
        for protocol in CONTRACTS:
            with self.subTest(protocol=protocol.__name__):
                self.assertTrue(issubclass(OneDrive, protocol))
                for name, declared in methods(protocol).items():
                    expected = inspect.signature(declared).parameters
                    actual = inspect.signature(getattr(OneDrive, name)).parameters
                    for parameter in expected.values():
                        self.assertIn(parameter.name, actual, f"{name} lacks {parameter.name}")
                        self.assertEqual(
                            actual[parameter.name].kind, parameter.kind, f"{name}.{parameter.name}"
                        )

    def test_test_fakes_cover_the_contracts_their_callers_use(self):
        self.assertTrue(issubclass(Catalog, backend.Metadata))
        self.assertTrue(issubclass(Catalog, backend.Transfer))
        self.assertTrue(issubclass(OfflineFake, backend.Transfer))

    def test_factory_is_the_only_construction_point(self):
        with patch("cloudmarks.onedrive.OneDrive") as provider:
            self.assertIs(backend.create("config", "settings"), provider.return_value)
        provider.assert_called_once_with("config", "settings")


class BoundaryTests(unittest.TestCase):
    CORE = (
        "controls",
        "engine",
        "folders",
        "offline",
        "recovery",
        "state",
        "storage",
        "tracking",
        "verification",
    )

    def test_core_modules_do_not_import_the_provider(self):
        import ast
        from pathlib import Path

        package = Path(backend.__file__).parent
        for name in self.CORE:
            tree = ast.parse((package / f"{name}.py").read_text())
            imported = {
                node.module
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom) and node.module and node.level
            }
            with self.subTest(module=name):
                self.assertNotIn("onedrive", imported)


if __name__ == "__main__":
    unittest.main()
