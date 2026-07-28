"""retire PostgreSQL vector storage after Qdrant migration

Revision ID: 0004_qdrant_vector_store
Revises: 0003_rag_v2_embeddings
"""

from __future__ import annotations

from alembic import op


revision = "0004_qdrant_vector_store"
down_revision = "0003_rag_v2_embeddings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Bring installations stamped against the older minimal source registry to
    # the current source contract before enabling the Qdrant-backed runtime.
    op.execute("ALTER TABLE sources ADD COLUMN IF NOT EXISTS name VARCHAR(256) NOT NULL DEFAULT ''")
    op.execute("ALTER TABLE sources ADD COLUMN IF NOT EXISTS status VARCHAR(64) NOT NULL DEFAULT 'inactive'")
    op.execute("ALTER TABLE sources ADD COLUMN IF NOT EXISTS sync_mode VARCHAR(64) NOT NULL DEFAULT 'manual'")
    op.execute("ALTER TABLE sources ADD COLUMN IF NOT EXISTS config_json JSONB")
    op.execute("ALTER TABLE sources ADD COLUMN IF NOT EXISTS credentials_ref TEXT")
    op.execute("ALTER TABLE sources ADD COLUMN IF NOT EXISTS last_sync_started_at TIMESTAMPTZ")
    op.execute("ALTER TABLE sources ADD COLUMN IF NOT EXISTS last_sync_finished_at TIMESTAMPTZ")
    op.execute("ALTER TABLE sources ADD COLUMN IF NOT EXISTS last_sync_status VARCHAR(64)")
    op.execute("ALTER TABLE sources ADD COLUMN IF NOT EXISTS last_error TEXT")
    op.execute("ALTER TABLE sources ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now()")
    op.execute("ALTER TABLE sources ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now()")

    op.execute("DROP TABLE IF EXISTS chunk_embeddings")
    op.execute("ALTER TABLE chunks DROP COLUMN IF EXISTS embedding")
    op.execute("DROP EXTENSION IF EXISTS vector")


def downgrade() -> None:
    raise NotImplementedError(
        "Restoring PostgreSQL vector storage requires an explicit reindex; roll forward with Qdrant instead."
    )
