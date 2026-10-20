#!/usr/bin/env python3
"""Check that rebuilding the sdist produces the same wheel payload."""

import argparse
import zipfile
from pathlib import Path


def payload(path):
    with zipfile.ZipFile(path) as archive:
        return {name: archive.read(name) for name in archive.namelist() if not name.endswith("/RECORD")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("first", type=Path)
    parser.add_argument("second", type=Path)
    args = parser.parse_args()
    if payload(args.first) != payload(args.second):
        parser.exit(1, "Rebuilt wheel payload differs from the original wheel\n")
    print("Source distribution rebuild matches the original wheel payload.")


if __name__ == "__main__":
    main()
