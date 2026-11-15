"""Sanctum Sanctorum — encrypting per-user API keys at rest.

Each user who wants the /chat AI links their OWN Gemini key (bring your
own key). Those keys are credentials, so they must never sit in the
database in plain text. This module encrypts them with Fernet
(AES-128-CBC + HMAC, via the `cryptography` library) before they are
stored, and decrypts them only when the owning user asks for /chat.

Where the encryption secret comes from, in order:
  1. The SANCTUM_KEY_SECRET environment variable (set it in .env for a
     deployment where you want the secret managed separately from the
     data directory). Keys encrypted with this secret stay readable as
     long as the variable is set.
  2. A file secret at <data_dir>/key_secret, generated once (32 random
     bytes, mode 0600) on first use. It lives next to the database it
     protects, so a backup/restore of the data directory keeps the keys
     readable, and a fresh data directory starts with a fresh secret
     (old keys simply become unreadable — the user re-links).

Decryption tries every available secret, so setting the environment
variable later does not lock out keys that were encrypted with the file
secret. If a stored token decrypts with no available secret, decrypt()
returns None (never the raw token, never a crash) and the caller treats
the user as having no key.

Nothing here ever logs a key value.
"""

from __future__ import annotations

import base64
import logging
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

log = logging.getLogger("sanctum.keystore")

# Fixed salt for the key derivation. The secrecy lives in the secret
# itself (a random 32-byte file secret, or the operator's env secret),
# not in the salt — the salt only has to be constant so the same secret
# always derives the same Fernet key.
_SALT = b"sanctum-key-vault-v1"
_ITERATIONS = 200_000
_SECRET_FILENAME = "key_secret"


def _derive(secret: bytes) -> Fernet:
    """Derive a Fernet key from an arbitrary secret with PBKDF2."""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_SALT,
        iterations=_ITERATIONS,
    )
    return Fernet(base64.urlsafe_b64encode(kdf.derive(secret)))


class KeyVault:
    """Encrypt/decrypt API keys with the available server secret(s)."""

    def __init__(self, data_dir: str | Path, env_secret: str | None = None):
        self._keys: list[Fernet] = []
        if env_secret:
            self._keys.append(_derive(env_secret.encode("utf-8")))
        secret_path = Path(data_dir) / _SECRET_FILENAME
        try:
            if secret_path.exists():
                # Read the secret EXACTLY as written: it is 32 raw random
                # bytes. The old code did read_bytes().strip() here, which
                # silently changed the key whenever those random bytes
                # happened to start or end with an ASCII whitespace byte
                # (~5% of fresh secrets) — every key encrypted before a
                # restart then became undecryptable. The exact bytes are
                # canonical now; the stripped derivation is also kept
                # (tried second) so keys encrypted by the old read path
                # stay readable.
                raw = secret_path.read_bytes()
                if raw:
                    self._keys.append(_derive(raw))
                    stripped = raw.strip()
                    if stripped and stripped != raw:
                        self._keys.append(_derive(stripped))
            else:
                raw = os.urandom(32)
                secret_path.parent.mkdir(parents=True, exist_ok=True)
                secret_path.write_bytes(raw)
                try:
                    os.chmod(secret_path, 0o600)
                except OSError:
                    pass
                self._keys.append(_derive(raw))
        except OSError as exc:
            # A data directory we cannot write to must not take the whole
            # bot down; keys simply cannot be stored until it is fixed.
            log.warning("KeyVault: cannot use file secret at %s: %s",
                        secret_path, exc)
        if not self._keys:
            # No secret at all (no env secret AND no usable file secret):
            # the vault is built, but encrypt() will refuse cleanly
            # instead of raising IndexError on self._keys[0].
            log.error(
                "KeyVault: no encryption secret is available — linked "
                "keys cannot be stored until SANCTUM_KEY_SECRET is set or "
                "the data directory is writable."
            )

    def encrypt(self, plaintext: str) -> str:
        """Encrypt a key; returns the Fernet token (URL-safe base64).
        Raises RuntimeError (never IndexError, never a log of the key)
        when no encryption secret is available."""
        if not self._keys:
            raise RuntimeError(
                "KeyVault: no encryption secret is available — cannot "
                "store a key. Set SANCTUM_KEY_SECRET or make the data "
                "directory writable."
            )
        return self._keys[0].encrypt(plaintext.encode("utf-8")).decode("utf-8")

    def decrypt(self, token: str) -> str | None:
        """Decrypt a stored token. Returns None if it decrypts with no
        available secret (e.g. the secret was lost) — never raises, and
        never returns the raw token."""
        if not token:
            return None
        for key in self._keys:
            try:
                return key.decrypt(token.encode("utf-8")).decode("utf-8")
            except (InvalidToken, ValueError):
                continue
        log.warning("KeyVault: a stored key token could not be decrypted "
                    "with any available secret; treating it as absent.")
        return None
