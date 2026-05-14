from __future__ import annotations

import re
from typing import Any

DISALLOWED_CONFIG_KEY_TOKENS = {
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "private_key",
    "client_secret",
    "access_key",
    "refresh_token",
    "bearer",
    "credential",
}

_SECRET_VALUE_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN\s+(RSA\s+|EC\s+|OPENSSH\s+)?PRIVATE KEY-----", re.IGNORECASE),
    re.compile(r"\bBearer\s+[A-Za-z0-9_\-.]{20,}\b", re.IGNORECASE),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
]

_ALLOWED_SENSITIVE_KEY_NAMES = {"credentials_ref"}


def find_source_config_secret_violations(config: dict[str, Any]) -> list[str]:
    violations: list[str] = []
    _walk_config(config, path=[], violations=violations)
    return violations


def _walk_config(value: Any, *, path: list[str], violations: list[str]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized_key = key.lower().replace("-", "_")
            current_path = path + [key]
            if normalized_key not in _ALLOWED_SENSITIVE_KEY_NAMES and any(
                token in normalized_key for token in DISALLOWED_CONFIG_KEY_TOKENS
            ):
                violations.append(f"{'.'.join(current_path)} contains a credential-like key")
                continue
            _walk_config(item, path=current_path, violations=violations)
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _walk_config(item, path=path + [str(index)], violations=violations)
        return
    if isinstance(value, str) and _looks_like_secret(value):
        location = ".".join(path) if path else "config"
        violations.append(f"{location} contains secret-like value")


def _looks_like_secret(value: str) -> bool:
    return any(pattern.search(value) for pattern in _SECRET_VALUE_PATTERNS)
