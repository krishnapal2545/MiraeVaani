"""
Encrypt/decrypt .env file for safe GitHub sharing.

Usage:
    python env_crypto.py encrypt    # .env → .env.encrypted (before git push)
    python env_crypto.py decrypt    # .env.encrypted → .env (after git pull)

Secret key is prompted interactively (never stored in code).
"""

import base64
import hashlib
import os
import sys
from getpass import getpass

# Using AES via cryptography library
try:
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
except ImportError:
    print("Missing dependency. Install with:")
    print("  pip install cryptography")
    sys.exit(1)

ENV_FILE = ".env"
ENCRYPTED_FILE = ".env.encrypted"
SALT = b"MiraeVaani4.0_salt_v1"  # Fixed salt (safe since password is strong)


def _get_key(password: str) -> bytes:
    """Derive a Fernet key from the password using PBKDF2."""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=SALT,
        iterations=480_000,
    )
    key = base64.urlsafe_b64encode(kdf.derive(password.encode()))
    return key


def encrypt():
    """Encrypt .env → .env.encrypted"""
    if not os.path.exists(ENV_FILE):
        print(f"ERROR: {ENV_FILE} not found.")
        sys.exit(1)

    password = getpass("Enter secret key: ")
    if not password:
        print("ERROR: Password cannot be empty.")
        sys.exit(1)

    key = _get_key(password)
    fernet = Fernet(key)

    with open(ENV_FILE, "rb") as f:
        data = f.read()

    encrypted = fernet.encrypt(data)

    with open(ENCRYPTED_FILE, "wb") as f:
        f.write(encrypted)

    print(f"✅ Encrypted {ENV_FILE} → {ENCRYPTED_FILE}")
    print(f"   File size: {len(encrypted)} bytes")
    print(f"   Safe to commit to GitHub.")


def decrypt():
    """Decrypt .env.encrypted → .env"""
    if not os.path.exists(ENCRYPTED_FILE):
        print(f"ERROR: {ENCRYPTED_FILE} not found. Pull from GitHub first.")
        sys.exit(1)

    password = getpass("Enter secret key: ")
    if not password:
        print("ERROR: Password cannot be empty.")
        sys.exit(1)

    key = _get_key(password)
    fernet = Fernet(key)

    with open(ENCRYPTED_FILE, "rb") as f:
        encrypted = f.read()

    try:
        decrypted = fernet.decrypt(encrypted)
    except Exception:
        print("ERROR: Wrong secret key or corrupted file.")
        sys.exit(1)

    with open(ENV_FILE, "wb") as f:
        f.write(decrypted)

    print(f"✅ Decrypted {ENCRYPTED_FILE} → {ENV_FILE}")
    print(f"   Your .env is ready to use.")


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in ("encrypt", "decrypt"):
        print("Usage:")
        print("  python env_crypto.py encrypt   # before git push")
        print("  python env_crypto.py decrypt   # after git pull")
        sys.exit(1)

    if sys.argv[1] == "encrypt":
        encrypt()
    else:
        decrypt()
