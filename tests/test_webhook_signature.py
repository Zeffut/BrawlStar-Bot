"""GitHub webhook HMAC-SHA256 signature verification.

This duplicates the inline check in cloud_panel/app.py so we can
test it in isolation without booting FastAPI (which requires py 3.10+).
"""
from __future__ import annotations

import hashlib
import hmac


def github_signature(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify(secret: str, body: bytes, header: str) -> bool:
    if not secret:
        return True  # no secret configured -> open
    expected = github_signature(secret, body)
    return hmac.compare_digest(header or "", expected)


def test_valid_signature_passes():
    body = b'{"action":"push","ref":"refs/heads/main"}'
    sig = github_signature("supersecret", body)
    assert verify("supersecret", body, sig)


def test_tampered_body_fails():
    body = b'{"ref":"refs/heads/main"}'
    sig = github_signature("supersecret", body)
    tampered = body + b' '
    assert not verify("supersecret", tampered, sig)


def test_wrong_secret_fails():
    body = b'{"x":1}'
    sig = github_signature("right", body)
    assert not verify("wrong", body, sig)


def test_missing_header_fails():
    assert not verify("supersecret", b'{}', "")


def test_no_secret_is_open():
    """If no secret is configured we don't validate (used in dev)."""
    assert verify("", b'{}', "")
