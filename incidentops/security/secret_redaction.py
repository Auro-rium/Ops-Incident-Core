from __future__ import annotations

import re

SECRET_PATTERNS = [
    (re.compile(r"\b(sk-[a-zA-Z0-9]{20,})\b"), "[REDACTED_API_KEY]"),
    (
        re.compile(r"\b(api[_-]?key\s*[:=]\s*['\"]?)([a-zA-Z0-9_\-]{16,})(['\"]?)", re.IGNORECASE),
        r"\1[REDACTED_API_KEY]\3",
    ),
    (re.compile(r"(Bearer\s+)([a-zA-Z0-9_\-.]{20,})", re.IGNORECASE), r"\1[REDACTED_TOKEN]"),
    (
        re.compile(r"\b(eyJ[a-zA-Z0-9_-]{10,}\.eyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,})\b"),
        "[REDACTED_JWT]",
    ),
    (re.compile(r"(password\s*[:=]\s*['\"]?)([^\s'\"]{8,})(['\"]?)", re.IGNORECASE), r"\1[REDACTED_PASSWORD]\3"),
    (
        re.compile(r"-----BEGIN\s+(RSA\s+)?PRIVATE KEY-----[\s\S]*?-----END\s+(RSA\s+)?PRIVATE KEY-----"),
        "[REDACTED_PRIVATE_KEY]",
    ),
]


def redact_secrets(text: str) -> str:
    result = text
    for pattern, replacement in SECRET_PATTERNS:
        result = pattern.sub(replacement, result)
    return result
