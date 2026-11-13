"""Review fixes: account lockout, case-insensitive emails, stale-document tracking."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "v2_0002_review_fixes"
down_revision = "v2_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    duplicates = bind.execute(
        sa.text("SELECT lower(email) FROM users GROUP BY lower(email) HAVING count(*) > 1")
    ).fetchall()
    if duplicates:
        raise RuntimeError(
            f"{len(duplicates)} user email(s) differ only by case; merge or rename them before upgrading"
        )
    op.execute("UPDATE users SET email = lower(email) WHERE email <> lower(email);")
    op.execute("CREATE UNIQUE INDEX uq_users_email_lower ON users (lower(email));")
    op.execute("ALTER TABLE users ADD COLUMN locked_until TIMESTAMP WITH TIME ZONE;")
    op.execute("ALTER TABLE documents ADD COLUMN last_seen_sync_id UUID;")
    op.execute("CREATE INDEX ix_documents_source_last_seen_sync ON documents (source_id, last_seen_sync_id);")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_documents_source_last_seen_sync;")
    op.execute("ALTER TABLE documents DROP COLUMN IF EXISTS last_seen_sync_id;")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS locked_until;")
    op.execute("DROP INDEX IF EXISTS uq_users_email_lower;")
