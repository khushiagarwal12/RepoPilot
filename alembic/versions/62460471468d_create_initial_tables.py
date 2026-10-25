"""create initial tables

Revision ID: ${revision_id}
Revises: None
Create Date: ${create_date}
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "62460471468d"       # <-- Alembic generated string
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # repositories table
    op.create_table(
        "repositories",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("github_url", sa.Text, nullable=False, unique=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), onupdate=sa.func.now()),
        sa.Column("active", sa.Boolean, nullable=False, server_default=sa.text("true")),
    )

    # ingestion_jobs table
    op.create_table(
        "ingestion_jobs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "repository_id",
            sa.Integer,
            sa.ForeignKey("repositories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum("pending", "running", "succeeded", "failed", name="job_status"),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("log", sa.Text, nullable=True),
    )


def downgrade() -> None:
    op.drop_table("ingestion_jobs")
    op.drop_table("repositories")