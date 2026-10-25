"""create code_definitions table

Revision ID: 73e721a91bc4
Revises: 62460471468d
Create Date: 2026-10-25 00:59:00.000000
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "73e721a91bc4"
down_revision = "62460471468d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "code_definitions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "repository_id",
            sa.Integer,
            sa.ForeignKey("repositories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("file_path", sa.Text, nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("definition_type", sa.String(length=50), nullable=False),
        sa.Column("start_line", sa.Integer, nullable=False),
        sa.Column("end_line", sa.Integer, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_code_definitions_repository_id", "code_definitions", ["repository_id"])
    op.create_index("ix_code_definitions_name", "code_definitions", ["name"])


def downgrade() -> None:
    op.drop_index("ix_code_definitions_name", table_name="code_definitions")
    op.drop_index("ix_code_definitions_repository_id", table_name="code_definitions")
    op.drop_table("code_definitions")
