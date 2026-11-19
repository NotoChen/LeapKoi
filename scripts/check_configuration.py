#!/usr/bin/env python3
"""Ensure the native and engine configuration defaults cannot drift."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile

from build_app import sdk_path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "Engine"))

from configuration import DEFAULTS, validate  # noqa: E402


def native_defaults() -> dict:
    with tempfile.TemporaryDirectory(prefix="approvaldesk-config-") as folder:
        binary = Path(folder) / "config-contract"
        subprocess.run(
            [
                "swiftc",
                "-sdk",
                sdk_path(),
                "-parse-as-library",
                str(root / "Sources/ApprovalDesk/Models.swift"),
                str(root / "Tools/ConfigContract.swift"),
                "-o",
                str(binary),
            ],
            check=True,
        )
        output = subprocess.run([str(binary)], check=True, capture_output=True, text=True).stdout
    return json.loads(output)


def main() -> int:
    expected = validate(DEFAULTS)
    actual = native_defaults()
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    changed = sorted(
        key for key in set(expected) & set(actual)
        if expected[key] != actual[key]
    )
    if not (missing or extra or changed):
        print(f"Configuration contract passed: {len(expected)} shared defaults")
        return 0
    if missing:
        print("Missing native defaults: " + ", ".join(missing), file=sys.stderr)
    if extra:
        print("Undeclared engine defaults: " + ", ".join(extra), file=sys.stderr)
    for key in changed:
        print(f"Default mismatch for {key}: engine={expected[key]!r} native={actual[key]!r}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
