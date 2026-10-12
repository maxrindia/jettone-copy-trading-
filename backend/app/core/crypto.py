import logging
from functools import lru_cache
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings

logger = logging.getLogger("crypto")


class EncryptionKeyError(RuntimeError):
    """Raised when ALICE_BLUE_ENCRYPTION_KEY is missing or invalid."""


@lru_cache(maxsize=1)
def _get_fernet() -> Fernet:
    key: Optional[str] = settings.ALICE_BLUE_ENCRYPTION_KEY
    if not key:
        raise EncryptionKeyError(
            "ALICE_BLUE_ENCRYPTION_KEY is not set; refusing to handle broker credentials."
        )
    try:
        return Fernet(key.encode("utf-8"))
    except (ValueError, TypeError) as exc:
        raise EncryptionKeyError(
            "ALICE_BLUE_ENCRYPTION_KEY is invalid. It must be a 32-byte urlsafe-base64 key "
            "(generate with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\")"
        ) from exc


def validate_encryption_key() -> None:
    """Explicit startup check; raises EncryptionKeyError if unusable."""
    _get_fernet()


def encrypt_secret(plaintext: str) -> str:
    return _get_fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_secret(ciphertext: str) -> str:
    try:
        return _get_fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        logger.critical("Credential decryption failed — wrong or rotated encryption key?")
        raise EncryptionKeyError(
            "Stored credential could not be decrypted with the current ALICE_BLUE_ENCRYPTION_KEY."
        ) from exc
