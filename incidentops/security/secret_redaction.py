from __future__ import annotations

import re

# The single secret pattern set shared by Core and the Collector. Order
# matters: specific token formats run before the generic key/value rules.
SECRET_PATTERNS = [
    (
        re.compile(r"-----BEGIN\s+(?:[A-Z0-9]+\s+)*PRIVATE KEY-----[\s\S]*?-----END\s+(?:[A-Z0-9]+\s+)*PRIVATE KEY-----"),
        "[REDACTED_PRIVATE_KEY]",
    ),
    (re.compile(r"\b(sk-[a-zA-Z0-9_-]{20,})\b"), "[REDACTED_API_KEY]"),
    (re.compile(r"\b(ghp_[a-zA-Z0-9_]{20,})\b"), "[REDACTED_GITHUB_TOKEN]"),
    (re.compile(r"\b(github_pat_[a-zA-Z0-9_]{20,})\b"), "[REDACTED_GITHUB_TOKEN]"),
    (re.compile(r"\b(AKIA[0-9A-Z]{16})\b"), "[REDACTED_CLOUD_ACCESS_KEY]"),
    (
        re.compile(r"\b(eyJ[a-zA-Z0-9_-]{10,}\.eyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,})\b"),
        "[REDACTED_JWT]",
    ),
    (re.compile(r"(Bearer\s+)([a-zA-Z0-9_\-.]{20,})", re.IGNORECASE), r"\1[REDACTED_TOKEN]"),
    (
        re.compile(r"\b(api[_-]?key\s*[:=]\s*['\"]?)([a-zA-Z0-9_\-]{8,})(['\"]?)", re.IGNORECASE),
        r"\1[REDACTED_API_KEY]\3",
    ),
    (re.compile(r"(password\s*[:=]\s*['\"]?)([^\s'\"]{4,})(['\"]?)", re.IGNORECASE), r"\1[REDACTED_PASSWORD]\3"),
]


def redact_secrets_with_count(text: str) -> tuple[str, int]:
    """Redact known secret formats and return the number of replacements."""
    result = text
    count = 0
    for pattern, replacement in SECRET_PATTERNS:
        result, replaced = pattern.subn(replacement, result)
        count += replaced
    return result, count


def redact_secrets(text: str) -> str:
    return redact_secrets_with_count(text)[0]
