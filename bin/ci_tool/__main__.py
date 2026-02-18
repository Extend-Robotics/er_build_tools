#!/usr/bin/env python3
"""Entry point for: python3 -m ci_tool or python3 /path/to/ci_tool"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ci_tool.cli import main  # noqa: E402

if __name__ == "__main__":
    main()
