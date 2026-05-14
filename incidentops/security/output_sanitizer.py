from __future__ import annotations

import re

from incidentops.security.secret_redaction import redact_secrets

SCRIPT_RE = re.compile(r"<script[\s\S]*?</script>", re.IGNORECASE)


def sanitize_output(text: str) -> str:
    sanitized = SCRIPT_RE.sub("[REMOVED_SCRIPT]", text)
    return redact_secrets(sanitized)
