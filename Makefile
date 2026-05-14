.PHONY: migrate migration-check db-current db-history db-downgrade

PYTHON ?= python
ALEMBIC ?= alembic

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
