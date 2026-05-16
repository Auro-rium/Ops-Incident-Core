from __future__ import annotations

import argparse
import asyncio
import os
import time
from pathlib import Path

import httpx


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local smoke test for IncidentOps using arbitrary data folders.")
    parser.add_argument("--data-path", required=True)
    parser.add_argument("--query", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--email", default="admin@incidentops.local")
    parser.add_argument("--password", default="incidentops")
    parser.add_argument("--top-k", type=int, default=8)
    parser.add_argument("--create-run", action="store_true")
    return parser


async def login_if_available(client: httpx.AsyncClient, email: str, password: str) -> str | None:
    try:
        response = await client.post("/v1/auth/login", json={"email": email, "password": password})
        response.raise_for_status()
        return response.json()["access_token"]
    except Exception:
        return None


async def create_project_with_fallback(
    client: httpx.AsyncClient,
    project_name: str,
    headers: dict[str, str],
) -> tuple[str, dict[str, str], bool]:
    if not headers:
        response = await client.post(
            "/v1/projects",
            json={"name": project_name, "demo_mode": True},
        )
        response.raise_for_status()
        return response.json()["project_id"], {}, True

    response = await client.post(
        "/v1/projects",
        headers=headers,
        json={"name": project_name, "demo_mode": False},
    )
    if response.status_code in {401, 403} and headers:
        response = await client.post(
            "/v1/projects",
            json={"name": project_name, "demo_mode": True},
        )
        response.raise_for_status()
        return response.json()["project_id"], {}, True
    response.raise_for_status()
    return response.json()["project_id"], headers, False


async def main() -> None:
    raise SystemExit(await run_smoke())


def _print_section(title: str) -> None:
    print(f"{title}")


async def run_smoke() -> int:
    args = build_parser().parse_args()
    data_path = str(Path(args.data_path).expanduser().resolve())
    headers = {}
    warnings: list[str] = []
    async with httpx.AsyncClient(base_url=args.base_url, timeout=240.0) as client:
        token = await login_if_available(client, args.email, args.password)
        auth_mode = "authenticated" if token else "anonymous"
        if token:
            headers["Authorization"] = f"Bearer {token}"
        else:
            warnings.append("auth login failed; using local demo project fallback if enabled")

        project_name = f"smoke-{os.urandom(4).hex()}"
        project_id, headers, anonymous_project = await create_project_with_fallback(client, project_name, headers)
        if anonymous_project:
            auth_mode = "anonymous_project"

        ingest_response = await client.post(
            f"/v1/projects/{project_id}/ingest",
            headers=headers,
            json={"path": data_path},
        )
        if not ingest_response.is_success:
            print(f"Ingestion failed: {ingest_response.status_code} {ingest_response.text[:400]}")
            return 1
        ingest_payload = ingest_response.json()
        if ingest_payload.get("chunks_created", 0) == 0:
            warnings.append("ingestion produced zero chunks")

        search_response = await client.post(
            "/v1/search",
            headers=headers,
            json={"project_id": project_id, "query": args.query, "top_k": args.top_k, "debug": True},
        )
        if not search_response.is_success:
            print(f"Search failed: {search_response.status_code} {search_response.text[:400]}")
            return 1
        search_payload = search_response.json()
        if search_payload.get("total", 0) == 0:
            warnings.append("search returned zero results")

        investigate_response = await client.post(
            "/v1/investigate",
            headers=headers,
            json={"project_id": project_id, "query": args.query, "top_k": args.top_k, "debug": True},
        )
        if not investigate_response.is_success:
            print(f"Investigate failed: {investigate_response.status_code} {investigate_response.text[:400]}")
            return 1
        investigate_payload = investigate_response.json()

        run_payload = None
        if args.create_run and token and not anonymous_project:
            run_response = await client.post(
                "/v1/runs",
                headers=headers,
                json={"project_id": project_id, "query": args.query, "top_k": args.top_k},
            )
            if run_response.is_success:
                run_payload = run_response.json()
                run_payload = await poll_run(client, headers, run_payload["run_id"])
            else:
                run_warnings = [
                    f"workflow run request failed with status {run_response.status_code}: {run_response.text[:200]}"
                ]
                warnings.extend(run_warnings)
                run_payload = {"warnings": run_warnings}

    if investigate_payload["likely_root_cause"]["confidence"] == "low":
        warnings.append("evidence is weak")
    if ingest_payload.get("skipped_files"):
        warnings.append(f"skipped_files={len(ingest_payload['skipped_files'])}")
    if anonymous_project:
        warnings.append("workflow run skipped because project was created without authenticated membership")

    _print_section("Auth")
    print(f"  mode: {auth_mode}")
    print()

    _print_section("Project")
    print(f"  project_id: {project_id}")
    print(f"  project_name: {project_name}")
    print()

    _print_section("Ingestion Summary")
    print(f"  total_files_seen: {ingest_payload['total_files_seen']}")
    print(f"  files_ingested: {ingest_payload['files_ingested']}")
    print(f"  files_skipped: {ingest_payload['files_skipped']}")
    print(f"  chunks_created: {ingest_payload['chunks_created']}")
    print(f"  source_type_counts: {ingest_payload.get('source_type_counts', {})}")
    print(f"  chunk_type_counts: {ingest_payload.get('chunk_type_counts', {})}")
    if ingest_payload.get("parser_errors"):
        print("  parser_errors:")
        for item in ingest_payload["parser_errors"]:
            print(f"    - {item['path']}: {item['error_summary']}")
    if ingest_payload.get("skipped_files"):
        print("  skipped_files:")
        for item in ingest_payload["skipped_files"][:10]:
            size = f" ({item['size_bytes']} bytes)" if item.get("size_bytes") is not None else ""
            print(f"    - {item['path']}: {item['reason']}{size}")
    print()

    _print_section("Source Coverage")
    coverage = ingest_payload.get("source_coverage", {})
    for key in ("has_logs", "has_code", "has_deploys", "has_incidents", "has_runbooks"):
        print(f"  {key}: {coverage.get(key)}")
    if coverage.get("warnings"):
        print("  warnings:")
        for item in coverage["warnings"]:
            print(f"    - {item}")
    print()

    _print_section("Search Results")
    print(f"  total: {search_payload.get('total', 0)}")
    for item in search_payload.get("results", [])[: min(5, len(search_payload.get('results', [])))]:
        print(f"  - {item['document_path']} [{item['source_type']}] score={item['score']}")
    if search_payload.get("debug"):
        print(f"  debug: {search_payload['debug']}")
    print()

    _print_section("Investigation")
    print(f"  task_type: {investigate_payload['task_type']}")
    print(f"  evidence_count: {len(investigate_payload.get('evidence', []))}")
    print(f"  root_cause: {investigate_payload['likely_root_cause']['summary']}")
    print(f"  confidence: {investigate_payload['confidence']}")
    if investigate_payload.get("confidence_reasons"):
        print("  confidence_reasons:")
        for item in investigate_payload["confidence_reasons"]:
            print(f"    - {item}")
    if investigate_payload.get("missing_data"):
        print("  missing_data:")
        for item in investigate_payload["missing_data"]:
            print(f"    - {item}")
    print()

    _print_section("Workflow Run")
    if run_payload and run_payload.get("run_id"):
        print(f"  run_id: {run_payload['run_id']}")
        print(f"  status: {run_payload.get('status')}")
    else:
        print("  not requested or unavailable")
    print()

    _print_section("Warnings")
    if warnings:
        for warning in warnings:
            print(f"  - {warning}")
    else:
        print("  none")
    if run_payload and run_payload.get("run_id"):
        pass
    if ingest_payload.get("chunks_created", 0) == 0 or search_payload.get("total", 0) == 0:
        return 1
    return 0


async def poll_run(client: httpx.AsyncClient, headers: dict[str, str], run_id: str, timeout_seconds: int = 120) -> dict:
    terminal = {"completed", "awaiting_approval", "waiting_for_approval", "failed", "rejected"}
    deadline = time.time() + timeout_seconds
    payload: dict = {"run_id": run_id, "status": "unknown"}
    while time.time() < deadline:
        response = await client.get(f"/v1/runs/{run_id}", headers=headers)
        if response.status_code == 404:
            await asyncio.sleep(1)
            continue
        if not response.is_success:
            return payload
        payload = response.json()
        if payload.get("status") in terminal:
            return payload
        await asyncio.sleep(2)
    payload["warning"] = "timed out waiting for workflow run"
    return payload


if __name__ == "__main__":
    asyncio.run(main())
