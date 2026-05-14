from __future__ import annotations

import uuid


def make_trace_id() -> str:
    return uuid.uuid4().hex
