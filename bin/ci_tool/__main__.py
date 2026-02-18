#!/usr/bin/env python3
"""Entry point for: python3 -m ci_tool or python3 /path/to/ci_tool"""
import os
import subprocess
import sys

ci_tool_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(ci_tool_dir))

requirements_file = os.path.join(ci_tool_dir, "requirements.txt")
subprocess.check_call(
    [sys.executable, "-m", "pip", "install", "--user", "--quiet", "-r", requirements_file]
)

from ci_tool.cli import main  # noqa: E402

if __name__ == "__main__":
    main()
