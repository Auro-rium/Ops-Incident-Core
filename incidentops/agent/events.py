from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from incidentops.db.models import AgentRunEvent


async def append_run_event(
    db: AsyncSession,
    run_id,
    event_type: str,
    node_name: str | None = None,
    payload: dict | None = None,
) -> AgentRunEvent:
    result = await db.execute(
        select(func.coalesce(func.max(AgentRunEvent.sequence_no), 0)).where(AgentRunEvent.run_id == run_id)
    )
    next_seq = int(result.scalar_one() or 0) + 1
    event = AgentRunEvent(
        run_id=run_id,
        sequence_no=next_seq,
        event_type=event_type,
        node_name=node_name,
        payload_json=payload or {},
    )
    db.add(event)
    await db.flush()
    return event
