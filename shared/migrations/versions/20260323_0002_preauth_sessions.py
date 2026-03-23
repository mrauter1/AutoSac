"""Add preauth sessions for login CSRF.

Revision ID: 20260323_0002
Revises: 20260323_0001
Create Date: 2026-03-23 00:10:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260323_0002"
down_revision = "20260323_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "preauth_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("csrf_token", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("ip_address", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_preauth_sessions")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_preauth_sessions_token_hash")),
    )


def downgrade() -> None:
    op.drop_table("preauth_sessions")
