"""Shared pytest fixtures + sys.path setup."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"

# Make repo modules importable from tests/ without installing.
for p in (ROOT, ROOT / "cloud_panel"):
    sp = str(p)
    if sp not in sys.path:
        sys.path.insert(0, sp)
