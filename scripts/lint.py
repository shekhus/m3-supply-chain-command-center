"""Run ruff then mypy; exit non-zero if either fails. Equivalent of `make lint`."""

from __future__ import annotations

import subprocess
import sys

COMMANDS = [
    [sys.executable, "-m", "ruff", "check", "."],
    [sys.executable, "-m", "mypy", "."],
]


def main() -> int:
    status = 0
    for cmd in COMMANDS:
        print("$", " ".join(cmd[1:]), flush=True)
        status |= subprocess.call(cmd)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
