from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import time
from pathlib import Path

import httpx


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="API-only production smoke test for IncidentOps Core.")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--query", required=True)
    parser.add_argument("--data-path", help="Optional local/dev folder path for compatibility smoke.")
    parser.add_argument("--use-collector-batch", default="true", choices=["true", "false"])
    parser.add_argument("--timeout-seconds", type=int, default=120)
    parser.add_argument("--top-k", type=int, default=6)
    return parser


async def main() -> None:
    raise SystemExit(await run_smoke())


async def run_smoke() -> int:
    args = build_parser().parse_args()
    deadline = time.time() + args.timeout_seconds
    warnings: list[str] = []

    async with httpx.AsyncClient(base_url=args.base_url.rstrip("/"), timeout=60.0) as client:
        health = await client.get("/health")
        if not health.is_success:
            print(f"health failed: {health.status_code} {health.text[:300]}")
            return 1
        ready = await client.get("/ready")
        if not ready.is_success:
            print(f"ready failed: {ready.status_code} {ready.text[:500]}")
            return 1

        login = await client.post("/v1/auth/login", json={"email": args.email, "password": args.password})
        if not login.is_success:
            print(f"login failed: {login.status_code} {login.text[:300]}")
            return 1
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

        project_name = f"prod-smoke-{os.urandom(4).hex()}"
        project = await client.post(
            "/v1/projects",
            headers=headers,
            json={"name": project_name, "demo_mode": False},
        )
        if not project.is_success:
            print(f"project create failed: {project.status_code} {project.text[:400]}")
            return 1
        project_id = project.json()["project_id"]

        if args.use_collector_batch == "true":
            ingest_summary = await _collector_batch_flow(client, headers, project_id, args.query)
        elif args.data_path:
            ingest_summary = await _local_ingest_flow(client, headers, project_id, args.data_path)
        else:
            print("--data-path is required when --use-collector-batch=false")
            return 1
        if ingest_summary["chunks_created"] <= 0:
            print("batch ingest created zero chunks")
            return 1

        search = await client.post(
            "/v1/search",
            headers=headers,
            json={"project_id": project_id, "query": args.query, "top_k": args.top_k, "debug": True},
        )
        if not search.is_success:
            print(f"search failed: {search.status_code} {search.text[:400]}")
            return 1
        search_payload = search.json()
        if search_payload.get("total", 0) == 0:
            print("search returned zero evidence")
            return 1

        investigate = await client.post(
            "/v1/investigate",
            headers=headers,
            json={"project_id": project_id, "query": args.query, "top_k": args.top_k, "debug": True},
        )
        if not investigate.is_success:
            print(f"investigate failed: {investigate.status_code} {investigate.text[:400]}")
            return 1
        investigation_payload = investigate.json()

        run = await client.post(
            "/v1/runs",
            headers=headers,
            json={"project_id": project_id, "query": args.query, "top_k": args.top_k},
        )
        if not run.is_success:
            print(f"workflow run create failed: {run.status_code} {run.text[:400]}")
            return 1
        run_payload = await _poll_run(client, headers, run.json()["run_id"], deadline)
        if run_payload.get("status") == "failed":
            print(f"workflow run failed: {run_payload.get('error')}")
            return 1

        eval_payload = await _run_eval_if_possible(client, headers, project_id, args.query, deadline, warnings)

    print("Production Smoke Summary")
    print(f"  project_id: {project_id}")
    print(f"  chunks_created: {ingest_summary['chunks_created']}")
    print(f"  search_results: {search_payload.get('total', 0)}")
    print(f"  top_evidence_paths: {[item['document_path'] for item in search_payload.get('results', [])[:3]]}")
    print(f"  investigation_confidence: {investigation_payload.get('confidence')}")
    print(f"  investigation_summary: {investigation_payload.get('likely_root_cause', {}).get('summary')}")
    print(f"  run_id: {run_payload.get('run_id')}")
    print(f"  run_status: {run_payload.get('status')}")
    if eval_payload:
        print(f"  eval_status: {eval_payload.get('status')}")
        print(f"  eval_summary: {eval_payload.get('summary', {})}")
    if warnings:
        print("  warnings:")
        for warning in warnings:
            print(f"    - {warning}")
    return 0


async def _collector_batch_flow(client: httpx.AsyncClient, headers: dict[str, str], project_id: str, query: str) -> dict:
    source = await client.post(
        f"/v1/projects/{project_id}/sources",
        headers=headers,
        json={
            "name": "smoke normalized evidence",
            "source_type": "filesystem",
            "sync_mode": "manual",
            "config": {"description": "Tiny smoke-test evidence batch"},
        },
    )
    source.raise_for_status()
    source_id = source.json()["id"]
    collector = await client.post(
        f"/v1/projects/{project_id}/collectors/register",
        headers=headers,
        json={"name": "smoke-prod", "environment": "smoke", "version": "0.1.0"},
    )
    collector.raise_for_status()
    collector_id = collector.json()["collector_id"]
    sync = await client.post(
        f"/v1/sources/{source_id}/syncs/start",
        headers=headers,
        json={"collector_id": collector_id, "diagnostics": {"total_files_seen": 3}},
    )
    sync.raise_for_status()
    sync_id = sync.json()["sync_id"]
    documents = _tiny_documents(query)
    batch = await client.post(
        f"/v1/sources/{source_id}/documents/batch",
        headers=headers,
        json={"sync_id": sync_id, "collector_id": collector_id, "documents": documents},
    )
    batch.raise_for_status()
    payload = batch.json()
    finish = await client.post(
        f"/v1/sources/{source_id}/syncs/{sync_id}/finish",
        headers=headers,
        json={
            "status": "success",
            "diagnostics": {"files_seen": 3, "files_skipped": 0, "parser_errors": len(payload.get("errors", []))},
            "coverage": payload.get("coverage", {}),
        },
    )
    finish.raise_for_status()
    return payload


async def _local_ingest_flow(client: httpx.AsyncClient, headers: dict[str, str], project_id: str, data_path: str) -> dict:
    ingest = await client.post(
        f"/v1/projects/{project_id}/ingest",
        headers=headers,
        json={"path": str(Path(data_path).expanduser().resolve())},
    )
    ingest.raise_for_status()
    payload = ingest.json()
    return {"chunks_created": payload.get("chunks_created", 0)}


async def _poll_run(client: httpx.AsyncClient, headers: dict[str, str], run_id: str, deadline: float) -> dict:
    terminal = {"completed", "awaiting_approval", "waiting_for_approval", "failed", "rejected"}
    last_payload: dict = {}
    while time.time() < deadline:
        response = await client.get(f"/v1/runs/{run_id}", headers=headers)
        if response.status_code == 404:
            await asyncio.sleep(1)
            continue
        response.raise_for_status()
        last_payload = response.json()
        if last_payload.get("status") in terminal:
            return last_payload
        await asyncio.sleep(2)
    raise RuntimeError(f"timed out waiting for run {run_id}; last_status={last_payload.get('status')}")


async def _run_eval_if_possible(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    project_id: str,
    query: str,
    deadline: float,
    warnings: list[str],
) -> dict | None:
    cases_path = _write_tiny_eval_case(query)
    try:
        response = await client.post(
            "/v1/evals/run",
            headers=headers,
            json={"project_id": project_id, "cases_path": str(cases_path), "top_k": 3},
        )
        if not response.is_success:
            warnings.append(f"eval skipped/failed: {response.status_code} {response.text[:200]}")
            return None
        payload = response.json()
        while payload.get("status") in {"queued", "running"} and time.time() < deadline:
            await asyncio.sleep(2)
            followup = await client.get(f"/v1/evals/{payload['eval_run_id']}", headers=headers)
            if followup.status_code == 404:
                continue
            followup.raise_for_status()
            payload = followup.json()
        return payload
    finally:
        try:
            cases_path.unlink()
        except OSError:
            pass


def _write_tiny_eval_case(query: str) -> Path:
    path = Path.cwd() / f".incidentops-smoke-eval-{os.urandom(4).hex()}.jsonl"
    case = {
        "id": "smoke_case",
        "question": query,
        "expected_documents": ["logs/service.log"],
        "expected_terms": ["timeout", "deploy"],
        "forbidden_terms": ["database outage"],
    }
    path.write_text(json.dumps(case) + "\n", encoding="utf-8")
    return path


def _tiny_documents(query: str) -> list[dict]:
    contents = {
        "logs/service.log": (
            "2026-05-05T10:00:00Z INFO tiny-service deploy=deadbee GET /v1/items latency=120ms\n"
            f"2026-05-05T10:05:00Z WARN tiny-service deploy=deadbee query={query!r} "
            "latency=940ms timeout waiting for catalog dependency\n"
        ),
        "deploys/deploy-deadbee.json": json.dumps(
            {
                "deploy_hash": "deadbee",
                "service": "tiny-service",
                "changed_at": "2026-05-05T10:03:00Z",
                "changes": ["added synchronous catalog dependency check in request path"],
            }
        ),
        "docs/runbook.md": (
            "# Tiny Service Latency Runbook\n"
            "If latency rises after deploy, compare deploy metadata with timeout logs and dependency changes.\n"
        ),
    }
    source_types = {
        "logs/service.log": "logs",
        "deploys/deploy-deadbee.json": "deploy",
        "docs/runbook.md": "runbook",
    }
    documents = []
    for path, content in contents.items():
        documents.append(
            {
                "external_id": path,
                "path": path,
                "source_type": source_types[path],
                "content": content,
                "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "metadata": {"service_name": "tiny-service", "environment": "smoke"},
                "size_bytes": len(content.encode("utf-8")),
                "modified_at": "2026-05-05T10:05:00Z",
            }
        )
    return documents


if __name__ == "__main__":
    asyncio.run(main())
