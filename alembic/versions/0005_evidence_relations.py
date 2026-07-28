"""add deterministic evidence relation graph

Revision ID: 0005_evidence_relations
Revises: 0004_qdrant_vector_store
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "0005_evidence_relations"
down_revision = "0004_qdrant_vector_store"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "evidence_relations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("evidence_chunk_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("relation_type", sa.String(length=64), nullable=False),
        sa.Column("from_key", sa.String(length=512), nullable=False),
        sa.Column("to_key", sa.String(length=512), nullable=False),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["evidence_chunk_id"], ["chunks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "project_id",
            "evidence_chunk_id",
            "relation_type",
            "from_key",
            "to_key",
            name="uq_evidence_relations_edge",
        ),
    )
    op.create_index("ix_evidence_relations_project_from", "evidence_relations", ["project_id", "from_key"])
    op.create_index("ix_evidence_relations_project_to", "evidence_relations", ["project_id", "to_key"])
    op.create_index("ix_evidence_relations_project_type", "evidence_relations", ["project_id", "relation_type"])


def downgrade() -> None:
    op.drop_table("evidence_relations")
