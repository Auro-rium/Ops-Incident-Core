from __future__ import annotations

import re

_PATTERNS = (
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("github_token", re.compile(r"\b(?:ghp|github_pat)_[A-Za-z0-9_]{20,}\b")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private_key", re.compile(r"-----BEGIN (?:RSA )?PRIVATE KEY-----[\s\S]*?-----END (?:RSA )?PRIVATE KEY-----")),
)


def redact(text: str) -> tuple[str, int]:
    count = 0
    for name, pattern in _PATTERNS:
        def replacement(_: re.Match[str], label: str = name) -> str:
            nonlocal count
            count += 1
            return f"[REDACTED:{label}]"
        text = pattern.sub(replacement, text)
    return text, count
