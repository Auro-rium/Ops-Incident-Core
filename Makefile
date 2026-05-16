.PHONY: migrate migration-check db-current db-history db-downgrade worker smoke-prod metrics-check

PYTHON ?= python
ALEMBIC ?= alembic
API_BASE_URL ?= http://127.0.0.1:8000
SMOKE_EMAIL ?= admin@incidentops.local
SMOKE_PASSWORD ?= incidentops

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
