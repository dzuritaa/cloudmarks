"""CLI deployment round trip and doctor run only against a disposable HOME."""

import contextlib
import io
import sys
import unittest
from unittest.mock import patch

from cloudmarks.__main__ import main
from scripts.check_operation import check


class OperationTests(unittest.TestCase):
    def test_install_enable_doctor_disable_uninstall_reinstall_preserves_state(self):
        def run_cli(*args):
            output = io.StringIO()
            with (
                patch.object(sys, "argv", ["cloudmarks", *map(str, args)]),
                contextlib.redirect_stdout(output),
            ):
                try:
                    main()
                except SystemExit as error:
                    self.assertEqual(error.code, 0, output.getvalue())
            return output.getvalue()

        result = check(run_cli=run_cli)
        self.assertEqual(result["round_trip"], "passed")
        self.assertTrue(result["doctor"].startswith("passed"))
        self.assertIn(result["units"].split(":")[0], ("passed", "skipped"))
