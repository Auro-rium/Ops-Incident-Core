.PHONY: migrate migration-check db-current db-history db-downgrade worker mcp-server smoke-prod metrics-check docker-build azure-login-check azure-build-push azure-deploy azure-migrate azure-bootstrap-admin azure-smoke azure-teardown

PYTHON ?= python
ALEMBIC ?= alembic
API_BASE_URL ?= http://127.0.0.1:8000
SMOKE_EMAIL ?= admin@incidentops.local
SMOKE_PASSWORD ?= incidentops
DOCKER_IMAGE ?= incidentops-core:local
AZURE_RESOURCE_GROUP ?=
AZURE_LOCATION ?=
ACR_NAME ?=
NAME_PREFIX ?=
ENVIRONMENT_NAME ?=
CORS_ORIGINS ?=
IMAGE_TAG ?= $(shell git rev-parse --short HEAD)

migrate:
	$(ALEMBIC) upgrade head

migration-check:
	$(PYTHON) scripts/check_migrations.py

db-current:
	$(ALEMBIC) current

db-history:
	$(ALEMBIC) history

db-downgrade:
	$(ALEMBIC) downgrade -1

worker:
	$(PYTHON) -m incidentops.worker

mcp-server:
	$(PYTHON) -m incidentops.mcp.server

smoke-prod:
	$(PYTHON) scripts/smoke_prod.py --base-url $(API_BASE_URL) --email $(SMOKE_EMAIL) --password $(SMOKE_PASSWORD) --query "What does this tiny service evidence say?"

metrics-check:
	$(PYTHON) -c "import httpx; c=httpx.Client(base_url='$(API_BASE_URL)', timeout=10); token=c.post('/v1/auth/login', json={'email':'$(SMOKE_EMAIL)','password':'$(SMOKE_PASSWORD)'}).json()['access_token']; r=c.get('/metrics', headers={'Authorization':f'Bearer {token}'}); print(r.text[:500]); r.raise_for_status()"

docker-build:
	docker build -f docker/core.Dockerfile -t $(DOCKER_IMAGE) .

azure-login-check:
	AZURE_RESOURCE_GROUP=$(AZURE_RESOURCE_GROUP) AZURE_LOCATION=$(AZURE_LOCATION) ./scripts/azure_login_check.sh

azure-build-push:
	AZURE_RESOURCE_GROUP=$(AZURE_RESOURCE_GROUP) AZURE_LOCATION=$(AZURE_LOCATION) ACR_NAME=$(ACR_NAME) IMAGE_TAG=$(IMAGE_TAG) ./scripts/azure_build_push_images.sh

azure-deploy:
	AZURE_RESOURCE_GROUP=$(AZURE_RESOURCE_GROUP) AZURE_LOCATION=$(AZURE_LOCATION) ACR_NAME=$(ACR_NAME) NAME_PREFIX=$(NAME_PREFIX) ENVIRONMENT_NAME=$(ENVIRONMENT_NAME) CORS_ORIGINS=$(CORS_ORIGINS) IMAGE_TAG=$(IMAGE_TAG) ./scripts/azure_deploy.sh

azure-migrate:
	AZURE_RESOURCE_GROUP=$(AZURE_RESOURCE_GROUP) NAME_PREFIX=$(NAME_PREFIX) ./scripts/azure_run_migrations.sh

azure-bootstrap-admin:
	AZURE_RESOURCE_GROUP=$(AZURE_RESOURCE_GROUP) NAME_PREFIX=$(NAME_PREFIX) ./scripts/azure_bootstrap_admin.sh

azure-smoke:
	AZURE_RESOURCE_GROUP=$(AZURE_RESOURCE_GROUP) NAME_PREFIX=$(NAME_PREFIX) ./scripts/azure_smoke.sh

azure-teardown:
	AZURE_RESOURCE_GROUP=$(AZURE_RESOURCE_GROUP) ./scripts/azure_teardown.sh
