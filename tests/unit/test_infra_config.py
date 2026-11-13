"""Static and script-level checks for deployment config that has no other test path."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
BICEP = (ROOT / "infra/azure/main.bicep").read_text(encoding="utf-8")
DEPLOY_SCRIPT = (ROOT / "scripts/azure_deploy.sh").read_text(encoding="utf-8")
WORKFLOW = yaml.safe_load((ROOT / ".github/workflows/deploy-azure.yml").read_text(encoding="utf-8"))
DOCKERFILE = (ROOT / "docker/core.Dockerfile").read_text(encoding="utf-8")


def _env_file(name: str) -> dict[str, str]:
    values = {}
    for line in (ROOT / name).read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key] = value
    return values


def _bicep_resource(symbol: str) -> str:
    match = re.search(rf"^resource {symbol} .*?^}}", BICEP, re.M | re.S)
    assert match, symbol
    return match.group(0)


# ── Bicep ────────────────────────────────────


def test_database_url_encodes_credentials():
    assert "${uriComponent(postgresAdminUser)}:${uriComponent(postgresAdminPassword)}@" in BICEP


def test_redis_host_comes_from_the_resource():
    assert "value: redisCache.properties.hostName" in BICEP.split("name: 'REDIS_HOST'")[1][:60]


def test_require_azure_openai_follows_the_provider():
    block = BICEP.split("name: 'REQUIRE_AZURE_OPENAI'")[1][:80]
    assert "cloudProvider == 'azure' ? 'true' : 'false'" in block


def test_trusted_proxies_is_configured():
    assert "param trustedProxies string" in BICEP
    assert "value: trustedProxies" in BICEP.split("name: 'TRUSTED_PROXIES'")[1][:60]


def test_mcp_app_gets_the_inbound_token_and_stays_down_without_it():
    app = _bicep_resource("coreMcp")
    assert "name: 'MCP_INBOUND_TOKEN'" in app
    assert "secretRef: 'incidentops-mcp-inbound-token'" in app
    assert "empty(incidentopsMcpToken) || empty(mcpInboundToken) ? 0 : 1" in app
    assert "name: 'incidentops-mcp-inbound-token'" in BICEP.split("var mcpSecrets")[1].split("var coreSecrets")[0]


def test_probe_job_declares_the_secret_the_smoke_script_references():
    assert "name: 'incidentops-mcp-inbound-token'" in _bicep_resource("mcpProbeJob")
    smoke = (ROOT / "scripts/azure_mcp_smoke.sh").read_text(encoding="utf-8")
    assert "secretref:incidentops-mcp-inbound-token" in smoke


def test_metrics_token_reaches_only_the_api():
    assert "name: 'METRICS_TOKEN'" in _bicep_resource("coreApi")
    assert "METRICS_TOKEN" not in BICEP.split("var sharedCoreEnv")[1].split("var mcpSecrets")[0]


def test_empty_new_secrets_use_the_placeholder_settings_treat_as_unset():
    assert "empty(mcpInboundToken) ? 'disabled' : mcpInboundToken" in BICEP
    assert "empty(metricsToken) ? 'disabled' : metricsToken" in BICEP


def test_migration_job_does_not_require_the_vector_store():
    assert "alembic upgrade head && python scripts/check_migrations.py --skip-vector" in BICEP


def test_api_waits_for_migrations_instead_of_exiting():
    api = _bicep_resource("coreApi")
    assert "sh scripts/wait_for_migrations.sh" in api
    assert "exec uvicorn apps.api.main:app" in api
    assert "python scripts/check_migrations.py &&" not in api


def test_private_networking_is_opt_in_and_gates_public_access():
    assert "param enablePrivateNetworking bool = false" in BICEP
    assert (
        "resource postgresAllowAzureServices 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules"
        "@2023-12-01-preview' = if (!enablePrivateNetworking)" in BICEP
    )
    for symbol in ("keyVault", "postgresServer", "redisCache"):
        assert "publicNetworkAccess: publicNetworkAccess" in _bicep_resource(symbol)
    assert "var publicNetworkAccess = enablePrivateNetworking ? 'Disabled' : 'Enabled'" in BICEP
    for symbol in ("vnet", "privateDnsZones", "privateDnsVnetLinks", "privateEndpoints", "privateDnsZoneGroups"):
        assert re.search(rf"^resource {symbol} .*if \(enablePrivateNetworking\)", BICEP, re.M), symbol


def test_every_deploy_parameter_exists_in_bicep():
    mapping = re.search(r"param_names = \{(.*?)^\}", DEPLOY_SCRIPT, re.M | re.S).group(1)
    names = re.findall(r'"(\w+)": "\w+"', mapping)
    assert names
    for name in names:
        assert re.search(rf"^param {name} ", BICEP, re.M), name
    for name in ("mcpInboundToken", "metricsToken", "trustedProxies", "enablePrivateNetworking"):
        assert name in names


def test_deploy_script_refuses_mcp_without_inbound_token():
    assert 'if [[ -n "$INCIDENTOPS_MCP_TOKEN" && -z "$MCP_INBOUND_TOKEN" ]]; then' in DEPLOY_SCRIPT


# ── workflow ─────────────────────────────────


def _steps():
    for job in WORKFLOW["jobs"].values():
        yield from job.get("steps", [])


def _deploy_step(name: str) -> dict:
    return next(step for step in WORKFLOW["jobs"]["deploy"]["steps"] if step.get("name") == name)


def test_workflow_never_interpolates_secrets_or_vars_into_scripts():
    for step in _steps():
        script = step.get("run", "")
        assert "${{ secrets." not in script, step.get("name")
        assert "${{ vars." not in script, step.get("name")


def test_deploy_job_has_no_redundant_event_filter():
    assert "if" not in WORKFLOW["jobs"]["deploy"]


def test_workflow_passes_provider_and_new_secrets_to_deploy():
    env = _deploy_step("Deploy Azure resources")["env"]
    for key in (
        "NVIDIA_CHAT_MODEL",
        "NVIDIA_CHAT_ENDPOINT",
        "MCP_INBOUND_TOKEN",
        "METRICS_TOKEN",
        "TRUSTED_PROXIES",
        "ENABLE_PRIVATE_NETWORKING",
    ):
        assert key in env, key
    assert "CLOUD_PROVIDER" in WORKFLOW["jobs"]["deploy"]["env"]


def test_test_jobs_do_not_inherit_the_production_provider():
    # A non-local provider without API keys would change how the CI API behaves.
    assert "CLOUD_PROVIDER" not in WORKFLOW["env"]
    assert "CLOUD_PROVIDER" not in WORKFLOW["jobs"]["core-tests"]["env"]


def test_workflow_requires_provider_secrets_conditionally():
    script = _deploy_step("Validate required variables")["run"]
    azure = script.split('if [[ "$CLOUD_PROVIDER" == "azure" ]]; then')[1].split("fi")[0]
    assert "AZURE_OPENAI_API_KEY" in azure
    assert "AZURE_OPENAI_ENDPOINT" in azure
    assert "AZURE_OPENAI_CHAT_DEPLOYMENT" in azure
    nvidia = script.split('if [[ "$CLOUD_PROVIDER" == "nvidia" ]]; then')[1].split("fi")[0]
    assert "NVIDIA_API_KEY" in nvidia
    before = script.split('if [[ "$CLOUD_PROVIDER" == "azure" ]]; then')[0]
    assert "AZURE_OPENAI" not in before


def test_mcp_smoke_no_longer_receives_the_core_service_token():
    assert "MCP_TOKEN" not in _deploy_step("Private MCP smoke")["env"]


def test_core_tests_run_a_queue_variant_with_a_worker():
    job = WORKFLOW["jobs"]["core-tests"]
    legs = {leg["mode"]: leg for leg in job["strategy"]["matrix"]["include"]}
    assert job["strategy"]["fail-fast"] is False
    assert legs["queue"]["worker_mode"] == "queue"
    assert legs["queue"]["job_queue_backend"] == "redis"
    assert legs["queue"]["rate_limit_backend"] == "redis"
    assert legs["inline"]["worker_mode"] == "inline"
    assert job["env"]["WORKER_MODE"] == "${{ matrix.worker_mode }}"
    assert job["env"]["JOB_QUEUE_BACKEND"] == "${{ matrix.job_queue_backend }}"
    assert job["env"]["RATE_LIMIT_BACKEND"] == "${{ matrix.rate_limit_backend }}"
    worker = next(step for step in job["steps"] if step.get("name") == "Start worker")
    assert worker["if"] == "matrix.mode == 'queue'"
    assert "python -m incidentops.worker" in worker["run"]


# ── env examples ─────────────────────────────


def test_production_env_example_matches_the_nvidia_deployment():
    env = _env_file(".env.production.example")
    assert env["CLOUD_PROVIDER"] == "nvidia"
    assert env["REQUIRE_AZURE_OPENAI"] == "false"
    assert env["REDIS_PORT"] == "10000"
    for key in ("NVIDIA_CHAT_MODEL", "NVIDIA_CHAT_ENDPOINT", "TRUSTED_PROXIES", "METRICS_TOKEN", "MCP_INBOUND_TOKEN"):
        assert key in env, key


def test_local_env_example_documents_new_settings():
    env = _env_file(".env.example")
    for key in ("TRUSTED_PROXIES", "METRICS_TOKEN", "MCP_INBOUND_TOKEN"):
        assert key in env, key


# ── Docker ───────────────────────────────────


def test_image_installs_from_the_lockfile():
    assert "uv.lock" in DOCKERFILE
    assert "uv sync --frozen --no-dev" in DOCKERFILE
    assert "--no-editable" in DOCKERFILE
    assert "pip wheel" not in DOCKERFILE
    assert re.search(r"uv==\d+\.\d+\.\d+", DOCKERFILE)
    ignored = (ROOT / ".dockerignore").read_text(encoding="utf-8").split()
    assert "uv.lock" not in ignored


# ── wait_for_migrations.sh ───────────────────


def _run_wait_script(tmp_path: Path, exit_code: int) -> tuple[subprocess.CompletedProcess, int]:
    calls = tmp_path / "calls"
    stub = tmp_path / "python"
    stub.write_text(f'#!/bin/sh\necho "$@" >> "{calls}"\nexit {exit_code}\n', encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    result = subprocess.run(
        ["sh", str(ROOT / "scripts/wait_for_migrations.sh")],
        capture_output=True,
        text=True,
        env={
            "PATH": f"{tmp_path}:/usr/bin:/bin",
            "MIGRATION_WAIT_MAX_SECONDS": "1",
            "MIGRATION_WAIT_SECONDS": "0",
        },
    )
    return result, len(calls.read_text(encoding="utf-8").splitlines())


def test_wait_script_stops_as_soon_as_migrations_are_ready(tmp_path):
    result, calls = _run_wait_script(tmp_path, 0)
    assert result.returncode == 0
    assert calls == 1
    assert "WARNING" not in result.stdout + result.stderr


def test_wait_script_gives_up_without_failing_the_container(tmp_path):
    # The migration job only runs after the deployment finishes, so an API that
    # exits here would block the deployment that the job is waiting on.
    result, calls = _run_wait_script(tmp_path, 1)
    assert result.returncode == 0
    assert calls >= 2
    assert "WARNING" in result.stderr


# ── alembic with an encoded DATABASE_URL ─────


def test_alembic_accepts_a_percent_encoded_database_url():
    # Bicep percent-encodes the password. Alembic's config parser treats a bare
    # "%" as interpolation syntax, so env.py has to escape it.
    env = {
        **os.environ,
        "DATABASE_URL": "postgresql+asyncpg://user:p%40ss%2Fw%25rd@db.example:5432/incidentops",
    }
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "v2_baseline", "--sql"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stderr[-800:]
    assert "CREATE TABLE projects" in result.stdout


# ── smoke_prod.py ────────────────────────────


@pytest.fixture
def smoke_prod():
    spec = importlib.util.spec_from_file_location("smoke_prod_under_test", ROOT / "scripts/smoke_prod.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_smoke_eval_sends_cases_inline(smoke_prod, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"eval_run_id": "e1", "status": "completed", "summary": {}})

    async with httpx.AsyncClient(base_url="http://core.test", transport=httpx.MockTransport(handler)) as client:
        warnings: list[str] = []
        payload = await smoke_prod._run_eval_if_possible(client, {}, "p1", "why?", 0.0, warnings)

    assert payload["status"] == "completed"
    assert warnings == []
    assert "cases_path" not in bodies[0]
    assert bodies[0]["cases"][0]["question"] == "why?"
    assert list(tmp_path.iterdir()) == []


def test_smoke_eval_case_passes_the_route_validation(smoke_prod):
    from apps.api.routes.evals import _validate_inline_cases
    from incidentops.config.settings import Settings

    cases = [smoke_prod._tiny_eval_case("why?")]
    assert _validate_inline_cases(cases, Settings(_env_file=None)) == cases
