#!/usr/bin/env python3
"""Validate local Markdown links and evidence JSON; historical hashes stay historical."""

import json
import re
from pathlib import Path
from urllib.parse import unquote


def main():
    root = Path(__file__).resolve().parents[1]
    documents = [*root.glob("*.md"), *(root / "docs").rglob("*.md"), *(root / "tests").rglob("README.md")]
    for path in documents:
        for target in re.findall(r"\]\(([^)]+)\)", path.read_text()):
            if "://" in target or target.startswith("#"):
                continue
            relative = unquote(target.split("#")[0])
            resolved = (path.parent / relative).resolve()
            if not resolved.is_relative_to(root) or not resolved.exists():
                raise ValueError(f"Broken/escaping local link in {path.relative_to(root)}: {target}")
    for path in (root / "docs/evidence").rglob("*.json"):
        json.loads(path.read_text())
    print("Local documentation links and evidence JSON passed.")


if __name__ == "__main__":
    main()
