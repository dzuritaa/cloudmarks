#!/usr/bin/env python3
"""Install one wheel in a disposable venv and run tests outside the checkout."""

import argparse
import os
import shutil
import subprocess
import tempfile
import venv
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1]
    wheel = args.wheel.resolve(strict=True)
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in ("PYTHONPATH", "PYTHONHOME") and not key.startswith("RCLONE_")
    }
    with tempfile.TemporaryDirectory(prefix="cm-wheel-") as temporary:
        work = Path(temporary)
        target = work / "environment"
        venv.create(target, with_pip=True)
        python = str(target / "bin/python")

        def run(*arguments, env=environment):
            subprocess.run([python, *arguments], cwd=work, env=env, check=True)

        run("-m", "pip", "install", "--no-deps", "--no-index", str(wheel))
        run(
            "-I",
            "-c",
            "import cloudmarks,pathlib,sys; assert pathlib.Path(cloudmarks.__file__).is_relative_to(sys.prefix)",
        )
        run("-I", "-m", "cloudmarks", "--version")
        run("-I", "-m", "cloudmarks", "demo")
        run("-I", "-m", "cloudmarks", "offline", "--help")
        for directory in ("tests", "examples", "scripts"):
            shutil.copytree(
                source / directory, work / directory, ignore=shutil.ignore_patterns("__pycache__")
            )
        run(
            "-m",
            "unittest",
            "discover",
            "-s",
            "tests",
            "-q",
            env=dict(environment, PYTHONWARNINGS="error::ResourceWarning"),
        )
        print("Installed wheel suite passed with the source checkout excluded from imports.")


if __name__ == "__main__":
    main()
