"""add durable operational agent runs, findings, and sanitized events

Revision ID: 0006_operational_agents
Revises: 0005_evidence_relations
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "0006_operational_agents"
down_revision = "0005_evidence_relations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    operational_run_status = postgresql.ENUM(
        "queued",
        "running",
        "completed",
        "failed",
        name="operationalrunstatus",
        create_type=False,
    )
    operational_run_status.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "operational_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_type", sa.String(length=64), nullable=False),
        sa.Column("status", operational_run_status, nullable=False, server_default="queued"),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("summary_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("model_call_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("project_id", "run_type", "idempotency_key", name="uq_operational_runs_idempotency"),
    )
    op.create_index("ix_operational_runs_project_created", "operational_runs", ["project_id", "created_at"])
    op.create_index("ix_operational_runs_status_created", "operational_runs", ["status", "created_at"])
    op.create_index("ix_operational_runs_project_type", "operational_runs", ["project_id", "run_type"])
    op.create_table(
        "operational_findings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("operational_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("finding_type", sa.String(length=96), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="open"),
        sa.Column("evidence_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("threshold_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("recommended_action", sa.Text(), nullable=False),
        sa.Column("fingerprint", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["operational_run_id"], ["operational_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_operational_findings_project_created", "operational_findings", ["project_id", "created_at"])
    op.create_index("ix_operational_findings_run", "operational_findings", ["operational_run_id"])
    op.create_index("ix_operational_findings_project_severity", "operational_findings", ["project_id", "severity"])
    op.create_table(
        "operational_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("operational_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.Column("event_type", sa.String(length=96), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False, server_default="info"),
        sa.Column("payload_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["operational_run_id"], ["operational_runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_operational_events_project_created", "operational_events", ["project_id", "created_at"])
    op.create_index("ix_operational_events_run", "operational_events", ["operational_run_id"])
    op.create_index("ix_operational_events_category_created", "operational_events", ["category", "created_at"])


def downgrade() -> None:
    op.drop_table("operational_events")
    op.drop_table("operational_findings")
    op.drop_table("operational_runs")
    sa.Enum(name="operationalrunstatus").drop(op.get_bind(), checkfirst=True)
