#!/usr/bin/env python3
"""Dependency advisory poller — wraps scanner.py for poller contract.

This module provides the poller interface expected by the framework,
delegating the actual scanning logic to scanner.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

import scanner

POLLER_NAME = "dependency-advisory-watch"


def main() -> int:
    return scanner.main()


if __name__ == "__main__":
    sys.exit(main())
