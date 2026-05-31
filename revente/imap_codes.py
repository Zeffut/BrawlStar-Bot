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


def _imap_cfg() -> dict:
    """Read IMAP credentials from cfg/imap.toml (gitignored)."""
    import tomllib
    from pathlib import Path
    p = Path(__file__).resolve().parent.parent / "cfg" / "imap.toml"
    with p.open("rb") as f:
        return tomllib.load(f)


def _message_text(msg) -> str:
    """Flatten an email.message into text (handles multipart)."""
    if msg.is_multipart():
        return "".join(_message_text(part) for part in msg.get_payload())
    try:
        payload = msg.get_payload(decode=True)
        return payload.decode(errors="ignore") if payload else ""
    except Exception:
        return ""


def wait_for_code(since_epoch: float, timeout_s: float = 120, poll_s: float = 5,
                  mailbox: str | None = None) -> str | None:
    """Poll the IMAP inbox for a Supercell verification code in a mail that
    arrived at/after `since_epoch`. Returns the 6-digit code or None on timeout.

    `mailbox` overrides cfg['user'] (e.g. to read a per-account dedicated
    inbox while authenticating with a shared login)."""
    import email
    import imaplib
    import time
    from email.utils import parsedate_to_datetime

    cfg = _imap_cfg()
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            M = imaplib.IMAP4_SSL(cfg["host"], int(cfg.get("port", 993)))
            try:
                M.login(mailbox or cfg["user"], cfg["password"])
                M.select("INBOX")
                # Supercell senders vary; match broadly then date-filter.
                typ, data = M.search(None, '(OR FROM "supercell" SUBJECT "code")')
                for num in reversed((data[0] or b"").split()):
                    typ, msg_data = M.fetch(num, "(RFC822)")
                    msg = email.message_from_bytes(msg_data[0][1])
                    try:
                        ts = parsedate_to_datetime(msg.get("Date")).timestamp()
                    except Exception:
                        ts = since_epoch  # undated → accept
                    if ts + 1 < since_epoch:
                        continue  # older than our request
                    code = extract_supercell_code(
                        (msg.get("Subject") or "") + " " + _message_text(msg))
                    if code:
                        return code
            finally:
                M.logout()
        except Exception:
            pass
        time.sleep(poll_s)
    return None
