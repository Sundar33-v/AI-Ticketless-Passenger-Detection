"""Shared start-up for the command-line scripts.

``import _bootstrap`` (first, before any ``app`` import) puts the project root
on ``sys.path`` so ``python scripts/<name>.py`` works from any directory.
It also provides an ``ArgumentParser`` whose usage errors exit with code 1
(argparse's default 2 would clash with the scripts' documented exit codes).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

USAGE_EXIT = 1

# Messages contain characters such as "→"; never crash on a legacy console code page.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass


class ArgumentParser(argparse.ArgumentParser):
    """argparse parser that exits with :data:`USAGE_EXIT` on usage errors."""

    def error(self, message: str):  # type: ignore[override]
        self.print_usage(sys.stderr)
        self.exit(USAGE_EXIT, f"{self.prog}: error: {message}\n")
