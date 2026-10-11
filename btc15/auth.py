"""Ed25519 request signing for the Polymarket US API.

Scheme per docs.polymarket.us/api-reference/authentication:

    message   = f"{timestamp_ms}{METHOD}{path}"
    signature = base64(ed25519_sign(message))

    X-PM-Access-Key : key id
    X-PM-Timestamp  : unix milliseconds
    X-PM-Signature  : base64 signature

The server allows 30 seconds of clock skew. `path` excludes the query string
unless proven otherwise; `sign_query` flips that if the venue disagrees.
"""

from __future__ import annotations

import base64
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path


def check_keyfile(path: str = "keys.env") -> str | None:
    """Warn if the secret file is readable by anyone but its owner.

    The secret signs orders against real money. On a shared box or a Pi with
    default permissions, a 644 keys.env is readable by every account on the
    machine.
    """
    f = Path(path)
    if not f.exists():
        return None
    mode = f.stat().st_mode
    if mode & (stat.S_IRGRP | stat.S_IROTH | stat.S_IWGRP | stat.S_IWOTH):
        return (f"{path} is readable by other users on this machine "
                f"({oct(mode & 0o777)}). Fix with: chmod 600 {path}")
    return None


@dataclass
class Credentials:
    key_id: str
    secret_b64: str

    @classmethod
    def from_env(cls) -> "Credentials":
        warn = check_keyfile()
        if warn:
            print(f"WARNING: {warn}")
        key_id = os.environ.get("PM_KEY_ID", "").strip()
        # Strip quotes as well as whitespace: pasting the secret with quotes
        # into keys.env is the single most common setup mistake and the
        # resulting error, a base64 length complaint, says nothing useful.
        secret = os.environ.get("PM_SECRET", "").strip().strip('"\'')
        if not key_id or not secret:
            raise RuntimeError(
                "Set PM_KEY_ID and PM_SECRET. Create them at "
                "polymarket.us/developer after completing KYC in the app. "
                "The secret is shown once only."
            )
        return cls(key_id, secret)


class Signer:
    """Signs requests. Holds the private key in memory only."""

    def __init__(self, creds: Credentials, sign_query: bool = False) -> None:
        from cryptography.hazmat.primitives.asymmetric import ed25519

        raw = base64.b64decode(creds.secret_b64)
        if len(raw) not in (32, 64):
            raise ValueError(
                f"Decoded secret is {len(raw)} bytes; expected 32 (seed) or 64."
            )
        self._key = ed25519.Ed25519PrivateKey.from_private_bytes(raw[:32])
        self._key_id = creds.key_id
        self.sign_query = sign_query

    def headers(self, method: str, path: str, query: str = "") -> dict[str, str]:
        ts = str(int(time.time() * 1000))
        target = f"{path}?{query}" if (self.sign_query and query) else path
        message = f"{ts}{method.upper()}{target}".encode()
        sig = base64.b64encode(self._key.sign(message)).decode()
        return {
            "X-PM-Access-Key": self._key_id,
            "X-PM-Timestamp": ts,
            "X-PM-Signature": sig,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
