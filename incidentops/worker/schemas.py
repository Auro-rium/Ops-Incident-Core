from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


class Job(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    job_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    attempts: int = 0
    enqueued_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

