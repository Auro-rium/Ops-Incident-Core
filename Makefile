.PHONY: migrate migration-check db-current db-history db-downgrade worker mcp-server smoke-prod metrics-check docker-build aws-login-check aws-bootstrap-cicd aws-build-push aws-deploy aws-migrate aws-bootstrap-admin aws-model-preflight aws-promote aws-smoke aws-mcp-smoke aws-backup-qdrant azure-login-check azure-build-push azure-deploy azure-migrate azure-bootstrap-admin azure-smoke azure-teardown

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
AWS_REGION ?= us-east-1
TF_STATE_BUCKET ?=
TF_STATE_KEY ?= incidentops/production.tfstate

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
	docker build -t $(DOCKER_IMAGE) .

aws-login-check:
	AWS_REGION=$(AWS_REGION) ./scripts/aws_login_check.sh

aws-bootstrap-cicd:
	AWS_REGION=$(AWS_REGION) TF_STATE_BUCKET=$(TF_STATE_BUCKET) ./scripts/aws_bootstrap_cicd.sh

aws-build-push:
	AWS_REGION=$(AWS_REGION) IMAGE_TAG=$(IMAGE_TAG) ./scripts/aws_build_push_images.sh

aws-deploy:
	AWS_REGION=$(AWS_REGION) TF_STATE_BUCKET=$(TF_STATE_BUCKET) TF_STATE_KEY=$(TF_STATE_KEY) ./scripts/aws_deploy.sh

aws-migrate:
	AWS_REGION=$(AWS_REGION) ./scripts/aws_run_migrations.sh

aws-bootstrap-admin:
	AWS_REGION=$(AWS_REGION) ./scripts/aws_bootstrap_admin.sh

aws-model-preflight:
	AWS_REGION=$(AWS_REGION) ./scripts/aws_model_preflight.sh

aws-promote:
	AWS_REGION=$(AWS_REGION) ./scripts/aws_deploy_services.sh

aws-smoke:
	AWS_REGION=$(AWS_REGION) ./scripts/aws_smoke.sh

aws-mcp-smoke:
	AWS_REGION=$(AWS_REGION) PROJECT_ID=$(PROJECT_ID) ./scripts/aws_mcp_smoke.sh

aws-backup-qdrant:
	AWS_REGION=$(AWS_REGION) ./scripts/aws_backup_qdrant.sh

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
