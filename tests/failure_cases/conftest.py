"""Let the failure-case suite import the helpers in `tests/` by their bare module name.

The repo's convention is `from banned_terms import ...` rather than `from tests.banned_terms import ...`,
because adding `tests/__init__.py` would make mypy see every test module twice. This keeps that convention
working one directory down.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
