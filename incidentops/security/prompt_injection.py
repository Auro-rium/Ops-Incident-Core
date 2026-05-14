from __future__ import annotations

import re

SUSPICIOUS_PATTERNS = [
    re.compile(r"ignore previous instructions", re.IGNORECASE),
    re.compile(r"system prompt", re.IGNORECASE),
    re.compile(r"call tool", re.IGNORECASE),
    re.compile(r"exfiltrat", re.IGNORECASE),
    re.compile(r"run this command", re.IGNORECASE),
]


def inspect_untrusted_text(text: str) -> dict:
    matches = [pattern.pattern for pattern in SUSPICIOUS_PATTERNS if pattern.search(text)]
    return {
        "is_suspicious": bool(matches),
        "matches": matches,
    }
