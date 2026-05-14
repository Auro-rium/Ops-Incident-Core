from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from incidentops.db.migrations import check_database_ready


async def main() -> int:
    payload = await check_database_ready()
    ready = payload.get("ready") is True
    print(json.dumps({"status": "ready" if ready else "not_ready", **payload}, indent=2, default=str))
    return 0 if ready else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
