"""Tests for request signing. Verifies the signature a real server checks."""
import base64

import pytest

from btc15.auth import Credentials, Signer


def _keypair():
    from cryptography.hazmat.primitives.asymmetric import ed25519
    k = ed25519.Ed25519PrivateKey.generate()
    from cryptography.hazmat.primitives import serialization
    raw = k.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption())
    return k.public_key(), base64.b64encode(raw).decode()


def test_signature_verifies():
    """A server holding the public key must accept what we produce."""
    pub, secret = _keypair()
    s = Signer(Credentials("key-123", secret))
    h = s.headers("GET", "/v1/portfolio/positions")
    msg = f"{h['X-PM-Timestamp']}GET/v1/portfolio/positions".encode()
    pub.verify(base64.b64decode(h["X-PM-Signature"]), msg)


def test_signature_is_rejected_when_tampered():
    pub, secret = _keypair()
    s = Signer(Credentials("key-123", secret))
    h = s.headers("POST", "/v1/orders")
    wrong = f"{h['X-PM-Timestamp']}POST/v1/order".encode()
    from cryptography.exceptions import InvalidSignature
    with pytest.raises(InvalidSignature):
        pub.verify(base64.b64decode(h["X-PM-Signature"]), wrong)


def test_headers_present_and_timestamp_in_ms():
    import time
    _, secret = _keypair()
    h = Signer(Credentials("abc", secret)).headers("GET", "/x")
    assert h["X-PM-Access-Key"] == "abc"
    assert {"X-PM-Timestamp", "X-PM-Signature"} <= set(h)
    drift = abs(int(h["X-PM-Timestamp"]) - int(time.time() * 1000))
    assert drift < 2000, "timestamp must be unix milliseconds"


def test_accepts_64_byte_secret():
    """Some exports give the 64-byte seed+public form. Take the first 32."""
    from cryptography.hazmat.primitives.asymmetric import ed25519
    from cryptography.hazmat.primitives import serialization
    k = ed25519.Ed25519PrivateKey.generate()
    raw = k.private_bytes(encoding=serialization.Encoding.Raw,
                          format=serialization.PrivateFormat.Raw,
                          encryption_algorithm=serialization.NoEncryption())
    pub_raw = k.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw)
    s = Signer(Credentials("k", base64.b64encode(raw + pub_raw).decode()))
    h = s.headers("GET", "/v1/x")
    k.public_key().verify(
        base64.b64decode(h["X-PM-Signature"]),
        f"{h['X-PM-Timestamp']}GET/v1/x".encode())


def test_rejects_wrong_length_secret():
    with pytest.raises(ValueError):
        Signer(Credentials("k", base64.b64encode(b"tooshort").decode()))
