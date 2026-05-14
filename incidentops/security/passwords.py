from __future__ import annotations

import bcrypt
import hashlib
import hmac
import re

_BCRYPT_PREFIXES = ("$2a$", "$2b$", "$2y$")
_LEGACY_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")


def hash_password(password: str) -> str:
    password_bytes = password.encode("utf-8")
    return bcrypt.hashpw(password_bytes, bcrypt.gensalt(rounds=12)).decode("utf-8")


def verify_password(password: str, stored_hash: str, *, allow_legacy_sha256: bool = False) -> bool:
    if _is_bcrypt_hash(stored_hash):
        try:
            return bcrypt.checkpw(password.encode("utf-8"), stored_hash.encode("utf-8"))
        except ValueError:
            return False
    if allow_legacy_sha256 and is_legacy_sha256_hash(stored_hash):
        candidate = hashlib.sha256(password.encode("utf-8")).hexdigest()
        return hmac.compare_digest(candidate, stored_hash)
    return False


def needs_rehash(stored_hash: str) -> bool:
    if not _is_bcrypt_hash(stored_hash):
        return True
    return _bcrypt_rounds(stored_hash) < 12


def is_legacy_sha256_hash(stored_hash: str) -> bool:
    return bool(_LEGACY_SHA256_RE.match(stored_hash or ""))


def _is_bcrypt_hash(stored_hash: str) -> bool:
    return bool(stored_hash) and stored_hash.startswith(_BCRYPT_PREFIXES)


def _bcrypt_rounds(stored_hash: str) -> int:
    parts = stored_hash.split("$")
    if len(parts) < 4:
        return 0
    try:
        return int(parts[2])
    except ValueError:
        return 0
