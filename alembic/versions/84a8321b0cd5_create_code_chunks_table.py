"""create code_chunks table

Revision ID: 84a8321b0cd5
Revises: 73e721a91bc4
Create Date: 2026-10-25 01:27:00.000000
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "84a8321b0cd5"
down_revision = "73e721a91bc4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "code_chunks",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "repository_id",
            sa.Integer,
            sa.ForeignKey("repositories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("file_path", sa.Text, nullable=False),
        sa.Column("chunk_index", sa.Integer, nullable=False),
        sa.Column("chunk_type", sa.String(length=50), nullable=False),
        sa.Column("symbol_name", sa.String(length=255), nullable=True),
        sa.Column("start_line", sa.Integer, nullable=False),
        sa.Column("end_line", sa.Integer, nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_code_chunks_repository_id", "code_chunks", ["repository_id"])
    op.create_index("ix_code_chunks_symbol_name", "code_chunks", ["symbol_name"])


def downgrade() -> None:
    op.drop_index("ix_code_chunks_symbol_name", table_name="code_chunks")
    op.drop_index("ix_code_chunks_repository_id", table_name="code_chunks")
    op.drop_table("code_chunks")
