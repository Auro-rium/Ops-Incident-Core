"""Load the demo corpus on boot and rebuild vectors lost with the container disk.

Postgres outlives a restart but the in-container Qdrant does not, so an existing
project may have documents and no vectors.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import httpx

BASE_URL = f"http://127.0.0.1:{os.getenv('PORT', '10000')}"
QDRANT_URL = os.environ["QDRANT_URL"]
COLLECTION = os.environ["QDRANT_COLLECTION"]
PROJECT_NAME = os.getenv("DEMO_PROJECT_NAME", "demo")
SOURCE_NAME = "demo-fixtures"
CORPUS = os.getenv("DEMO_CORPUS_PATH", "seed/basic_incident")


def wait_for_api(client: httpx.Client) -> None:
    deadline = time.time() + 600
    while time.time() < deadline:
        try:
            if client.get("/health").status_code == 200:
                return
        except httpx.TransportError:
            pass
        time.sleep(2)
    raise RuntimeError("API did not become healthy")


def vector_count(project_id: str) -> int:
    response = httpx.post(
        f"{QDRANT_URL}/collections/{COLLECTION}/points/count",
        json={"filter": {"must": [{"key": "project_id", "match": {"value": project_id}}]}, "exact": True},
        timeout=30,
    )
    return response.json()["result"]["count"] if response.status_code == 200 else 0


def main() -> None:
    client = httpx.Client(base_url=BASE_URL, timeout=300)
    wait_for_api(client)
    login = client.post(
        "/v1/auth/login",
        json={"email": os.environ["BOOTSTRAP_ADMIN_EMAIL"], "password": os.environ["BOOTSTRAP_ADMIN_PASSWORD"]},
    )
    login.raise_for_status()
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    projects = client.get("/v1/projects", headers=headers).json()
    project_id = next((str(item["project_id"]) for item in projects if item["name"] == PROJECT_NAME), None)
    if project_id is None:
        created = client.post("/v1/projects", headers=headers, json={"name": PROJECT_NAME})
        created.raise_for_status()
        project_id = created.json()["project_id"]

    subprocess.run(
        [sys.executable, "-m", "incidentops.collector", "sync", CORPUS, "--project-id", project_id, "--source-name", SOURCE_NAME],
        check=True,
        env={**os.environ, "INCIDENTOPS_API_URL": BASE_URL, "INCIDENTOPS_TOKEN": token},
        stdout=subprocess.DEVNULL,
    )
    time.sleep(5)
    if vector_count(project_id) == 0:
        sources = client.get(f"/v1/projects/{project_id}/sources", headers=headers).json()
        source_id = next(source["id"] for source in sources if source["name"] == SOURCE_NAME)
        client.post(f"/v1/projects/{project_id}/sources/{source_id}/reindex", headers=headers).raise_for_status()
    print(f"demo seed finished project={project_id}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # The API stays useful without demo data, so a failed seed must not stop it.
        print(f"demo seed failed: {type(exc).__name__}", file=sys.stderr, flush=True)
