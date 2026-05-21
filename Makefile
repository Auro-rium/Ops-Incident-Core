.PHONY: migrate migration-check db-current db-history db-downgrade worker smoke-prod metrics-check docker-build terraform-fmt terraform-validate ec2-demo-deploy ec2-demo-smoke ec2-demo-backup ec2-demo-teardown

PYTHON ?= python
ALEMBIC ?= alembic
API_BASE_URL ?= http://127.0.0.1:8000
SMOKE_EMAIL ?= admin@incidentops.local
SMOKE_PASSWORD ?= incidentops
DOCKER_IMAGE ?= incidentops-core:local
EC2_PUBLIC_URL ?= http://127.0.0.1
EC2_COLLECTOR_REPO ?= ../Ops-Incident-Collector
EC2_FRONTEND_REPO ?= ../Ops-Incident-frontend

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

smoke-prod:
	$(PYTHON) scripts/smoke_prod.py --base-url $(API_BASE_URL) --email $(SMOKE_EMAIL) --password $(SMOKE_PASSWORD) --query "What does this tiny service evidence say?"

metrics-check:
	$(PYTHON) -c "import httpx; c=httpx.Client(base_url='$(API_BASE_URL)', timeout=10); token=c.post('/v1/auth/login', json={'email':'$(SMOKE_EMAIL)','password':'$(SMOKE_PASSWORD)'}).json()['access_token']; r=c.get('/metrics', headers={'Authorization':f'Bearer {token}'}); print(r.text[:500]); r.raise_for_status()"

docker-build:
	docker build -t $(DOCKER_IMAGE) .

terraform-fmt:
	terraform -chdir=infra/terraform fmt -recursive

terraform-validate:
	terraform -chdir=infra/terraform init -backend=false
	terraform -chdir=infra/terraform validate

ec2-demo-deploy:
	./scripts/deploy_ec2_demo.sh --public-url $(EC2_PUBLIC_URL) --collector-repo $(EC2_COLLECTOR_REPO) --frontend-repo $(EC2_FRONTEND_REPO)

ec2-demo-smoke:
	./scripts/smoke_ec2_demo.sh

ec2-demo-backup:
	./scripts/backup_db.sh

ec2-demo-teardown:
	./scripts/teardown_ec2_demo.sh
