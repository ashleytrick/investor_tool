"""B5 (CRM foundation): symmetric encryption for CRM api_key
storage.

Why Fernet: it's the canonical "I just want safe symmetric
encryption with the right defaults" primitive from `cryptography`
(AES-128-CBC + HMAC-SHA256, ciphertext is authenticated, includes
a version byte + timestamp). Beats hand-rolling AES-GCM.

Key handling:
  - `CRM_ENCRYPTION_KEY` env var, urlsafe-base64-encoded 32 bytes
    (the shape `cryptography.fernet.Fernet.generate_key()` returns).
  - Unset / wrong shape -> every encrypt / decrypt call raises
    `CRMSecretsMisconfigured`. The endpoints catch this and 500
    with a clear message rather than silently storing plaintext.

Rotation note: if you need to rotate the key, do it via Fernet's
`MultiFernet` -- store the new key first in the env var list,
decrypt with both, re-encrypt with the new one. We don't ship
that helper yet because the system has zero stored CRM keys at
launch; add it when there's an installed base to migrate.
"""
from __future__ import annotations

import os


class CRMSecretsMisconfigured(RuntimeError):
    """Raised when `CRM_ENCRYPTION_KEY` is unset or unparseable.
    Endpoint handlers translate this to a 500 with a clear
    operator-facing message."""


def _load_fernet():
    raw = os.environ.get("CRM_ENCRYPTION_KEY") or ""
    if not raw:
        raise CRMSecretsMisconfigured(
            "CRM_ENCRYPTION_KEY env var is unset; the CRM "
            "endpoints refuse to operate without an encryption "
            "key. Generate one with "
            "`python -c 'from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())'` and set it "
            "as a Fly secret."
        )
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:
        raise CRMSecretsMisconfigured(
            "cryptography library not installed -- "
            "`uv sync --extra api` should pull it in transitively"
        ) from exc
    try:
        return Fernet(raw.encode("ascii"))
    except Exception as exc:  # noqa: BLE001
        raise CRMSecretsMisconfigured(
            "CRM_ENCRYPTION_KEY is set but not a valid Fernet "
            "key (urlsafe-base64-encoded 32 bytes). Generate a "
            "fresh one with `Fernet.generate_key()`."
        ) from exc


def encrypt_api_key(plaintext: str) -> str:
    """Encrypt a CRM api_key for at-rest storage. Returns the
    ciphertext as a urlsafe-base64 ASCII string (the shape Fernet
    emits)."""
    if not plaintext:
        raise ValueError("encrypt_api_key: plaintext is empty")
    f = _load_fernet()
    return f.encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_api_key(ciphertext: str) -> str:
    """Decrypt a stored ciphertext back to the original api_key.
    Raises `CRMSecretsMisconfigured` when the encryption key is
    unset; raises `cryptography.fernet.InvalidToken` on tampered
    ciphertext."""
    if not ciphertext:
        raise ValueError("decrypt_api_key: ciphertext is empty")
    f = _load_fernet()
    return f.decrypt(ciphertext.encode("ascii")).decode("utf-8")


def key_suffix(plaintext: str, *, n: int = 4) -> str:
    """Display-only tail of the plaintext api_key. We surface this
    in the connection view so the operator can identify which
    credential is on file without ever sending the full key back
    through the API."""
    if not plaintext:
        return ""
    return plaintext[-n:]
