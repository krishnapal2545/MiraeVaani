"""Fernet encryption for API keys stored in SQLite.

Same PBKDF2 + Fernet approach as env_crypto.py, but keyed off APP_SECRET so the
app can encrypt and decrypt without an interactive passphrase prompt.
"""

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings

_SALT = b"miraevaani-v6-credentials"
_ITERATIONS = 200_000


def _fernet() -> Fernet:
    passphrase = get_settings().APP_SECRET.encode("utf-8")
    key = hashlib.pbkdf2_hmac("sha256", passphrase, _SALT, _ITERATIONS, dklen=32)
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt(plaintext: str) -> bytes:
    return _fernet().encrypt(plaintext.encode("utf-8"))


def decrypt(token: bytes) -> str:
    """Returns "" for anything that cannot be decrypted (e.g. APP_SECRET changed)."""
    if not token:
        return ""
    try:
        return _fernet().decrypt(bytes(token)).decode("utf-8")
    except (InvalidToken, ValueError):
        return ""


def mask(secret: str) -> str:
    """Display form for the UI: never send a full key back to the browser."""
    if not secret:
        return ""
    if len(secret) <= 8:
        return "*" * len(secret)
    return f"{secret[:4]}{'*' * 8}{secret[-4:]}"
