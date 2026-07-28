"""add index job storage and bridge legacy document metadata"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0003_rag_v2_embeddings"
down_revision = "0002_security_audit_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Some pre-baseline installations were stamped at 0002 before these
    # document fields were formalized. Keep their upgrade path valid while
    # remaining a no-op for fresh baseline installs.
    op.execute("ALTER TABLE documents ADD COLUMN IF NOT EXISTS external_id TEXT")
    op.execute("ALTER TABLE documents ADD COLUMN IF NOT EXISTS source_type VARCHAR(64)")
    op.execute("ALTER TABLE documents ADD COLUMN IF NOT EXISTS content_hash VARCHAR(128)")
    op.execute("ALTER TABLE documents ADD COLUMN IF NOT EXISTS modified_at TIMESTAMPTZ")
    op.execute("ALTER TABLE documents ADD COLUMN IF NOT EXISTS size_bytes INTEGER")
    op.execute("ALTER TABLE chunks ADD COLUMN IF NOT EXISTS timestamp_end TIMESTAMPTZ")

    op.create_index("ix_documents_project_source_type", "documents", ["project_id", "source_type"])
    op.create_index("ix_chunks_project_source_type", "chunks", ["project_id", "source_id", "chunk_type"])
    op.create_table(
        "index_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sync_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("normalized_payload_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("content_hash", sa.String(length=128), nullable=False),
        sa.Column("index_version", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["sync_id"], ["source_syncs.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_index_jobs_status_created", "index_jobs", ["status", "created_at"])
    op.create_index("ix_index_jobs_status_queued", "index_jobs", ["status", "queued_at"])
    op.create_index("ix_index_jobs_sync_id", "index_jobs", ["sync_id"])
    op.create_index("ix_index_jobs_source_version", "index_jobs", ["source_id", "index_version"])


def downgrade() -> None:
    op.drop_index("ix_chunks_project_source_type", table_name="chunks")
    op.drop_index("ix_documents_project_source_type", table_name="documents")
    op.drop_index("ix_index_jobs_source_version", table_name="index_jobs")
    op.drop_index("ix_index_jobs_sync_id", table_name="index_jobs")
    op.drop_index("ix_index_jobs_status_created", table_name="index_jobs")
    op.drop_index("ix_index_jobs_status_queued", table_name="index_jobs")
    op.drop_table("index_jobs")
