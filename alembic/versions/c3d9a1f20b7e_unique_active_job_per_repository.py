"""Allow at most one pending/running ingestion job per repository.

Revision ID: c3d9a1f20b7e
Revises: 84a8321b0cd5
"""
from alembic import op
import sqlalchemy as sa

revision = "c3d9a1f20b7e"
down_revision = "84a8321b0cd5"
branch_labels = None
depends_on = None

ACTIVE = sa.text("status IN ('pending', 'running')")


def upgrade() -> None:
    # Existing duplicate active jobs would violate the new index: keep the newest
    # active job of each repository and mark the older ones failed (nothing is deleted).
    op.execute(
        """
        UPDATE ingestion_jobs
        SET status = 'failed', log = 'Superseded by a newer active job (migration c3d9a1f20b7e)'
        WHERE status IN ('pending', 'running')
          AND id NOT IN (
              SELECT MAX(id) FROM ingestion_jobs
              WHERE status IN ('pending', 'running') GROUP BY repository_id
          )
        """
    )
    op.create_index(
        "uq_ingestion_jobs_one_active_per_repo",
        "ingestion_jobs",
        ["repository_id"],
        unique=True,
        postgresql_where=ACTIVE,
        sqlite_where=ACTIVE,
    )


def downgrade() -> None:
    op.drop_index("uq_ingestion_jobs_one_active_per_repo", table_name="ingestion_jobs")
