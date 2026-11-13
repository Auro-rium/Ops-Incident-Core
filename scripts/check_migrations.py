from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from incidentops.db.migrations import check_database_ready


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify the database schema is at the Alembic head.")
    parser.add_argument(
        "--skip-vector",
        action="store_true",
        help="Do not require the vector store (used by the migration job, which may run before Qdrant is up).",
    )
    return parser


async def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = await check_database_ready(skip_vector=args.skip_vector)
    ready = payload.get("ready") is True
    print(json.dumps({"status": "ready" if ready else "not_ready", **payload}, indent=2, default=str))
    return 0 if ready else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
