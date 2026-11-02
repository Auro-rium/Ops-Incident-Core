"""End-to-end checks against a live local stack in queue mode with async indexing.

Expects a running API, Postgres, Redis and Qdrant, a bootstrapped admin, and the
same environment the API uses, with INDEX_JOB_TIMEOUT_SECONDS low enough for the
crash check. This script starts and kills its own worker and MCP server, so no
other worker may be running. It never deploys and is meant for local embeddings.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import create_async_engine

from incidentops.db.models import Chunk, Document, IndexJob, IssueDraft

BASE_URL = os.getenv("API_BASE_URL", "http://127.0.0.1:8000")
QDRANT_URL = os.environ["QDRANT_URL"]
COLLECTION = os.environ["QDRANT_COLLECTION"]
ADMIN_EMAIL = os.getenv("E2E_ADMIN_EMAIL", "admin@incidentops.local")
ADMIN_PASSWORD = os.getenv("E2E_ADMIN_PASSWORD", "incidentops")
LOG_DIR = Path(os.getenv("E2E_LOG_DIR", tempfile.gettempdir()))
MCP_PORT = os.getenv("E2E_MCP_PORT", "8091")
REPO = Path(__file__).resolve().parents[1]

results: list[tuple[str, bool]] = []
client = httpx.Client(base_url=BASE_URL, timeout=120)
engine = create_async_engine(os.environ["DATABASE_URL"])


def login() -> tuple[str, dict[str, str]]:
    response = client.post("/v1/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    response.raise_for_status()
    token = response.json()["access_token"]
    return token, {"Authorization": f"Bearer {token}"}


TOKEN, HEADERS = login()


def check(name: str, ok: object, detail: object = "") -> None:
    results.append((name, bool(ok)))
    print(f"{'PASS' if ok else 'FAIL'} {name}: {detail}", flush=True)


def db_rows(statement) -> list:
    async def run() -> list:
        try:
            async with engine.connect() as connection:
                return list((await connection.execute(statement)).all())
        finally:
            await engine.dispose()

    return asyncio.run(run())


def create_project(prefix: str) -> str:
    response = client.post("/v1/projects", headers=HEADERS, json={"name": f"{prefix}-{uuid.uuid4().hex[:8]}"})
    response.raise_for_status()
    return response.json()["project_id"]


def collector_sync(path: Path, project_id: str, source: str, *extra: str) -> dict:
    env = {**os.environ, "INCIDENTOPS_API_URL": BASE_URL, "INCIDENTOPS_TOKEN": TOKEN}
    done = subprocess.run(
        [sys.executable, "-m", "incidentops.collector", "sync", str(path), "--project-id", project_id, "--source-name", source, *extra],
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO,
        timeout=500,
    )
    if done.returncode:
        raise RuntimeError(done.stderr[-1500:])
    return json.loads(done.stdout[done.stdout.index("{") :])


def start_process(log_name: str, *args: str, **env: str) -> subprocess.Popen:
    log = open(LOG_DIR / log_name, "a")
    return subprocess.Popen(
        [sys.executable, *args], cwd=REPO, stdout=log, stderr=subprocess.STDOUT, start_new_session=True, env={**os.environ, **env}
    )


def start_worker() -> subprocess.Popen:
    return start_process("e2e-worker.log", "-m", "incidentops.worker")


def stop_process(process: subprocess.Popen, sig: int = signal.SIGKILL) -> None:
    try:
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        return
    process.wait()


def job_counts(project_id: str) -> dict[str, int]:
    rows = db_rows(
        select(IndexJob.status, func.count()).where(IndexJob.project_id == uuid.UUID(project_id)).group_by(IndexJob.status)
    )
    return {str(getattr(status, "value", status)): count for status, count in rows}


def mark_one_job_stuck(project_id: str) -> uuid.UUID | None:
    async def run() -> uuid.UUID | None:
        try:
            async with engine.begin() as connection:
                job_id = (
                    await connection.execute(
                        select(IndexJob.id).where(IndexJob.project_id == uuid.UUID(project_id), IndexJob.status == "pending").limit(1)
                    )
                ).scalar_one_or_none()
                if job_id is not None:
                    await connection.execute(
                        update(IndexJob)
                        .where(IndexJob.id == job_id)
                        .values(status="running", started_at=datetime.now(timezone.utc) - timedelta(hours=1))
                    )
                return job_id
        finally:
            await engine.dispose()

    return asyncio.run(run())


def wait_for_jobs(project_id: str, timeout: int = 300) -> dict[str, int]:
    deadline = time.time() + timeout
    counts: dict[str, int] = {}
    while time.time() < deadline:
        counts = job_counts(project_id)
        if not {"pending", "queued", "running"} & set(counts):
            break
        time.sleep(1)
    return counts


def vector_count(project_id: str, **match: str) -> int:
    must = [{"key": "project_id", "match": {"value": project_id}}]
    must += [{"key": key, "match": {"value": value}} for key, value in match.items()]
    response = httpx.post(f"{QDRANT_URL}/collections/{COLLECTION}/points/count", json={"filter": {"must": must}, "exact": True})
    response.raise_for_status()
    return response.json()["result"]["count"]


def document_count(project_id: str) -> int:
    return db_rows(select(func.count()).select_from(Document).where(Document.project_id == uuid.UUID(project_id)))[0][0]


def document_id(project_id: str, path: str):
    rows = db_rows(select(Document.id).where(Document.project_id == uuid.UUID(project_id), Document.path == path))
    return rows[0][0] if rows else None


def build_corpus() -> Path:
    root = Path(tempfile.mkdtemp(prefix="e2e-corpus-"))
    shutil.copytree(REPO / "incidentops", root / "incidentops", ignore=shutil.ignore_patterns("__pycache__"))
    (root / "postmortems").mkdir()
    (root / "postmortems" / "2026-01-outage.md").write_text("# Outage\n\nservice: payments\n\nDatabase failover caused errors.\n")
    (root / "doomed.py").write_text("def doomed_function():\n    return 'delete me'\n")
    (root / "keeper.py").write_text("def keeper_function():\n    return 'keep me'\n")
    return root


def check_sync_and_pruning(corpus: Path, project_id: str) -> None:
    first = collector_sync(corpus, project_id, "self")
    jobs = wait_for_jobs(project_id)
    check("sync: index jobs finish", first.get("documents_created", 0) > 0 and set(jobs) == {"completed"}, jobs)
    rows = db_rows(
        select(Document.source_type, func.count())
        .where(Document.project_id == uuid.UUID(project_id), Document.path.like("incidentops/%"))
        .group_by(Document.source_type)
    )
    by_type = {source_type: count for source_type, count in rows}
    check("sync: incidentops/*.py routes as code, not incident", not by_type.get("incident") and by_type.get("code", 0) > 50, by_type)
    postmortem = db_rows(select(Document.source_type).where(Document.project_id == uuid.UUID(project_id), Document.path == "postmortems/2026-01-outage.md"))
    check("sync: postmortem routes as incident", postmortem and postmortem[0][0] == "incident", postmortem)
    check("sync: vectors published", vector_count(project_id) > 0, vector_count(project_id))

    second = collector_sync(corpus, project_id, "self")
    wait_for_jobs(project_id)
    check("re-sync: unchanged documents skipped", second.get("documents_unchanged", 0) > 0 and not second.get("documents_created"), second.get("documents_unchanged"))

    doomed = document_id(project_id, "doomed.py")
    (corpus / "doomed.py").unlink()
    before = document_count(project_id)
    collector_sync(corpus, project_id, "self", "--max-files", "2")
    wait_for_jobs(project_id)
    check("prune: --max-files sync deletes nothing", document_count(project_id) == before, f"{before} -> {document_count(project_id)}")
    collector_sync(corpus, project_id, "self")
    wait_for_jobs(project_id)
    check("prune: full sync removes the document", document_id(project_id, "doomed.py") is None, doomed)
    chunks = db_rows(select(func.count()).select_from(Chunk).where(Chunk.document_id == doomed))[0][0]
    check("prune: chunks removed", chunks == 0, chunks)
    check("prune: vectors removed", vector_count(project_id, document_id=str(doomed)) == 0, "")
    check("prune: other files survive", document_id(project_id, "keeper.py") is not None, "")


def check_retrieval(project_id: str) -> None:
    search = client.post("/v1/search", headers=HEADERS, json={"project_id": project_id, "query": "keeper_function", "top_k": 5})
    paths = [hit.get("document_path") for hit in search.json().get("results", [])] if search.is_success else []
    check("search: finds the function by name", "keeper.py" in paths, paths[:3])
    answer = client.post("/v1/answer", headers=HEADERS, json={"project_id": project_id, "query": "Where is keeper_function implemented?", "top_k": 5})
    check("answer: returns cited evidence", answer.is_success and answer.json().get("evidence"), answer.status_code)
    ready = client.get("/ready")
    check("ready: reports healthy", ready.status_code == 200, ready.status_code)


def check_reindex(project_id: str) -> None:
    sources = client.get(f"/v1/projects/{project_id}/sources", headers=HEADERS).json()
    source_id = next(source["id"] for source in sources if source["name"] == "self")
    response = client.post(f"/v1/projects/{project_id}/sources/{source_id}/reindex", headers=HEADERS)
    jobs = wait_for_jobs(project_id)
    check("reindex: accepted and jobs finish", response.is_success and set(jobs) == {"completed"}, response.status_code)
    check("reindex: vectors still present", vector_count(project_id) > 0, vector_count(project_id))


def check_crash_recovery(corpus: Path, worker: subprocess.Popen) -> subprocess.Popen:
    project_id = create_project("e2e-crash")
    with ThreadPoolExecutor(1) as pool:
        sync = pool.submit(collector_sync, corpus, project_id, "crash")
        deadline = time.time() + 60
        while time.time() < deadline and not job_counts(project_id):
            time.sleep(0.05)
        stop_process(worker)
        sync.result()
    # Local embeddings finish in milliseconds, so a kill rarely lands inside a job.
    # Leave the row a mid-job crash would: running, and long past its timeout.
    stuck = mark_one_job_stuck(project_id)
    at_restart = job_counts(project_id)
    check("crash: worker killed with work outstanding", stuck and at_restart.get("running") and at_restart.get("pending"), at_restart)
    worker = start_worker()
    jobs = wait_for_jobs(project_id)
    check("crash: every job recovers after restart", set(jobs) == {"completed"}, jobs)
    check("crash: recovered project has vectors", vector_count(project_id) > 0, vector_count(project_id))
    return worker


def start_run(project_id: str) -> tuple[str, str | None]:
    response = client.post(
        "/v1/runs",
        headers=HEADERS,
        json={"project_id": project_id, "query": "What changed in the last deploy?", "top_k": 3, "create_issue_draft": True},
    )
    response.raise_for_status()
    run_id = response.json()["run_id"]
    status = None
    deadline = time.time() + 120
    while time.time() < deadline:
        status = client.get(f"/v1/runs/{run_id}", headers=HEADERS).json()["status"]
        if status in {"awaiting_approval", "completed", "failed", "rejected"}:
            break
        time.sleep(0.5)
    return run_id, status


def check_approval(project_id: str) -> None:
    run_id, status = start_run(project_id)
    check("approval: run waits for a decision", status == "awaiting_approval", status)
    with client.stream("GET", f"/v1/runs/{run_id}/events", headers=HEADERS, params={"stream": "true"}) as stream:
        events = sum(1 for line in stream.iter_lines() if line.startswith("data:"))
    check("sse: stream delivers events and ends", events > 0, events)

    def approve(_: int) -> int:
        return httpx.post(f"{BASE_URL}/v1/runs/{run_id}/approve", headers=HEADERS, json={"rationale": "ok"}, timeout=60).status_code

    with ThreadPoolExecutor(4) as pool:
        codes = sorted(pool.map(approve, range(4)))
    check("approval: concurrent approvals give one 200, rest 409", codes == [200, 409, 409, 409], codes)
    check("approval: later repeat is 409", approve(0) == 409, "")
    final = client.get(f"/v1/runs/{run_id}", headers=HEADERS).json()["status"]
    check("approval: approved run completes", final == "completed", final)

    rejected_id, _ = start_run(project_id)
    rejected = client.post(f"/v1/runs/{rejected_id}/reject", headers=HEADERS, json={"rationale": "no"})
    check("approval: reject marks the run rejected", rejected.is_success and rejected.json()["status"] == "rejected", rejected.status_code)
    rows = db_rows(select(IssueDraft.run_id, IssueDraft.status).where(IssueDraft.run_id.in_([uuid.UUID(run_id), uuid.UUID(rejected_id)])))
    drafts = {str(key): str(getattr(value, "value", value)) for key, value in rows}
    check("approval: drafts marked approved and rejected", drafts.get(run_id) == "approved" and drafts.get(rejected_id) == "rejected", drafts)


def check_eval(project_id: str) -> None:
    cases = [{"id": "bad", "question": "keeper_function", "expected_terms": ["zzz-term-that-cannot-appear"], "forbidden_terms": []}]
    response = client.post("/v1/evals/run", headers=HEADERS, json={"project_id": project_id, "top_k": 3, "cases": cases})
    response.raise_for_status()
    eval_id = response.json()["eval_run_id"]
    body: dict = {}
    deadline = time.time() + 120
    while time.time() < deadline:
        body = client.get(f"/v1/evals/{eval_id}", headers=HEADERS).json()
        if body.get("status") in {"completed", "failed"}:
            break
        time.sleep(0.5)
    summary = body.get("summary") or {}
    check("eval: run completes", body.get("status") == "completed", body.get("status"))
    check("eval: a bad case fails", summary.get("passed_cases") == 0, summary.get("passed_cases"))


def check_mcp(project_id: str) -> None:
    inbound = uuid.uuid4().hex
    server = start_process(
        "e2e-mcp.log",
        "-m",
        "incidentops.mcp.server",
        MCP_TRANSPORT="streamable-http",
        MCP_HOST="127.0.0.1",
        MCP_PORT=MCP_PORT,
        MCP_INBOUND_TOKEN=inbound,
        MCP_TOKEN=TOKEN,
        MCP_CORE_API_URL=BASE_URL,
    )
    url = f"http://127.0.0.1:{MCP_PORT}/mcp"
    try:
        status = None
        deadline = time.time() + 30
        while time.time() < deadline and status is None:
            try:
                status = httpx.post(url, headers={"Authorization": "Bearer wrong-token"}, json={}).status_code
            except httpx.TransportError:
                time.sleep(0.5)
        check("mcp: wrong inbound token rejected", status == 401, status)
        probe = subprocess.run(
            [sys.executable, "scripts/mcp_probe.py"],
            capture_output=True,
            text=True,
            cwd=REPO,
            timeout=120,
            env={**os.environ, "PROJECT_ID": project_id, "MCP_URL": url, "MCP_INBOUND_TOKEN": inbound, "MCP_PROBE_QUERY": "keeper_function"},
        )
        report = json.loads(probe.stdout) if probe.returncode == 0 else {}
        check("mcp: probe lists tools and searches", report.get("search_result_count", 0) > 0, report or probe.stderr[-300:])
    finally:
        stop_process(server)


def check_token_revocation() -> None:
    token, headers = login()
    assert token
    before = client.get("/v1/auth/me", headers=headers).status_code
    response = client.post("/v1/auth/logout-all", headers=headers)
    after = client.get("/v1/auth/me", headers=headers).status_code
    check("auth: logout-all revokes the token", before == 200 and response.is_success and after == 401, (before, response.status_code, after))


def main() -> int:
    corpus = build_corpus()
    worker = start_worker()
    try:
        project_id = create_project("e2e-self")
        check_sync_and_pruning(corpus, project_id)
        check_retrieval(project_id)
        check_reindex(project_id)
        worker = check_crash_recovery(corpus, worker)
        check_approval(project_id)
        check_eval(project_id)
        check_mcp(project_id)
        check_token_revocation()
    finally:
        stop_process(worker)
        shutil.rmtree(corpus, ignore_errors=True)
    failed = [name for name, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    for name in failed:
        print(f"FAILED: {name}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
