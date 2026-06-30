"""Encryption boundary for secrets persisted in SQLite."""

from __future__ import annotations

from typing import Protocol

from cryptography.fernet import Fernet, InvalidToken
from pydantic import SecretStr


class SecretCodecError(RuntimeError):
    """Raised when a persisted secret cannot be safely encoded or decoded."""


class SecretCodec(Protocol):
    """Encode and decode secret values without exposing storage details."""

    def encode(self, value: str) -> str:
        """Return an encrypted representation suitable for persistence."""

    def decode(self, value: str) -> str:
        """Return the decrypted secret."""


class FernetSecretCodec:
    """Authenticated symmetric encryption for database-backed API keys."""

    def __init__(self, key: str | bytes) -> None:
        encoded_key = key.encode("utf-8") if isinstance(key, str) else key
        try:
            self._fernet = Fernet(encoded_key)
        except (TypeError, ValueError) as exc:
            raise SecretCodecError("CONFIG_ENCRYPTION_KEY is not a valid Fernet key") from exc

    @classmethod
    def from_secret(cls, key: SecretStr) -> FernetSecretCodec:
        """Create a codec from a Pydantic secret without rendering it."""

        return cls(key.get_secret_value())

    def encode(self, value: str) -> str:
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decode(self, value: str) -> str:
        try:
            return self._fernet.decrypt(value.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeDecodeError, ValueError) as exc:
            raise SecretCodecError("A database secret could not be decrypted") from exc
