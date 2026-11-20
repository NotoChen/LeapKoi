#!/usr/bin/env python3
from pathlib import Path
import os
import subprocess
import sys
from build_app import sdk_path

root = Path(__file__).resolve().parents[1]
output = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else root / ".cache/previews"
binary = root / ".cache/render-previews"
binary.parent.mkdir(parents=True, exist_ok=True)
sources = sorted(p for p in (root / "Sources/LeapKoi").glob("*.swift") if p.name != "LeapKoiApp.swift")
subprocess.run(["swiftc", "-sdk", sdk_path(), "-parse-as-library", *sources, root / "Tools/RenderPreviews.swift", "-o", binary], check=True)
arguments = [binary, output]
if len(sys.argv) > 2:
    arguments.append(Path(sys.argv[2]).resolve())
environment = os.environ.copy()
environment["LEAPKOI_RESOURCE_DIR"] = str(root / "Resources")
subprocess.run(arguments, check=True, env=environment)
