"""
Fernet-based symmetric encryption for storing vendor login passwords.
Uses SHA-256 of the app's secret_key as the Fernet key so any string
secret_key works (Fernet requires exactly 32 URL-safe base64 bytes).
"""
import base64
import hashlib

from cryptography.fernet import Fernet

from app.core.config import settings


def _fernet() -> Fernet:
    digest = hashlib.sha256(settings.secret_key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_password(plaintext: str) -> str:
    """Encrypt a plaintext password; returns a URL-safe token string."""
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_password(token: str) -> str:
    """Decrypt a Fernet token back to the original plaintext password."""
    return _fernet().decrypt(token.encode()).decode()
