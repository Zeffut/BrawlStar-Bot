"""Retrieve Supercell ID email verification codes via IMAP.

`extract_supercell_code` is pure (unit-tested). `wait_for_code` polls an
IMAP inbox and is exercised live (Phase 2). Credentials live in
cfg/imap.toml (gitignored) — never commit them.
"""
from __future__ import annotations

import re

# A standalone 6-digit number, preferring one that follows a "code" keyword.
_CODE_NEAR = re.compile(r"code[^0-9]{0,20}(\d{6})", re.IGNORECASE)
_ANY_6 = re.compile(r"(?<!\d)(\d{6})(?!\d)")


def extract_supercell_code(body: str) -> str | None:
    """Return the 6-digit Supercell verification code found in an email body."""
    m = _CODE_NEAR.search(body)
    if m:
        return m.group(1)
    m = _ANY_6.search(body)
    return m.group(1) if m else None
