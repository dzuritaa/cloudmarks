"""The benchmark's fake fixture retains production limits and bounded admission."""

import contextlib
import io
import json
import unittest

from scripts import bench_scale


class BenchmarkTests(unittest.TestCase):
    def test_help_invalid_count_and_reproducible_json(self):
        with contextlib.redirect_stdout(io.StringIO()) as output, self.assertRaises(SystemExit) as error:
            bench_scale.main(["--help"])
        self.assertEqual(error.exception.code, 0)
        self.assertIn("--entries", output.getvalue())
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            bench_scale.main(["--entries", "50001"])
        self.assertEqual(error.exception.code, 2)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            bench_scale.main(["--entries", "1000", "10000", "50000", "--rounds", "1", "--json"])
        value = json.loads(output.getvalue())
        self.assertEqual([v["entries"] for v in value["folders"]], [1000, 10000, 50000])
        self.assertEqual(
            [v["observer"]["state"] for v in value["folders"]], ["cloud_only", "cloud_only", "unknown"]
        )
        self.assertTrue(all(v["engine"]["state"] == "unknown" for v in value["folders"]))
        self.assertTrue(all(v["desktop"]["accepted"] == 512 for v in value["folders"]))
        self.assertTrue(all(v["desktop"]["deferred"] == v["entries"] - 512 for v in value["folders"]))
