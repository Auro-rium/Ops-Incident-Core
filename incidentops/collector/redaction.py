from __future__ import annotations

from incidentops.security.secret_redaction import redact_secrets_with_count


def redact(text: str) -> tuple[str, int]:
    """Redact with the same pattern set Core applies before prompts and output."""
    return redact_secrets_with_count(text)
