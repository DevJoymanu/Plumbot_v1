"""
At-rest encryption for tenant credentials (docs/MULTI_TENANT_PLAN.md §3.1).

Fernet with a key derived from settings.SECRET_KEY — no new secrets to manage,
no new dependencies (cryptography is already a requirement). Values are stored
with a `fernet:` prefix so plaintext legacy rows keep working: decrypt_secret()
passes anything unprefixed straight through, and the next save re-encrypts.

Rotating SECRET_KEY invalidates stored ciphertexts — re-enter channel tokens
after a rotation (they're recoverable from Meta, nothing is lost).
"""

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings

_PREFIX = 'fernet:'


def _fernet() -> Fernet:
    digest = hashlib.sha256(settings.SECRET_KEY.encode('utf-8')).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_secret(plaintext: str) -> str:
    """Encrypt a credential for storage. Empty values stay empty; already
    encrypted values are returned unchanged (idempotent)."""
    if not plaintext or plaintext.startswith(_PREFIX):
        return plaintext or ''
    token = _fernet().encrypt(plaintext.encode('utf-8')).decode('ascii')
    return _PREFIX + token


def decrypt_secret(value: str) -> str:
    """Decrypt a stored credential. Unprefixed (legacy plaintext) values pass
    through unchanged. An undecryptable prefixed value (SECRET_KEY rotated)
    returns '' — callers treat that as credential-missing, never crash."""
    if not value:
        return ''
    if not value.startswith(_PREFIX):
        return value
    try:
        return _fernet().decrypt(value[len(_PREFIX):].encode('ascii')).decode('utf-8')
    except (InvalidToken, ValueError):
        return ''
