"""Add preauth login session store.

Revision ID: 20260324_0002
Revises: 20260323_0001
Create Date: 2026-03-24 00:00:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260324_0002"
down_revision = "20260323_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "preauth_login_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("csrf_token", sa.Text(), nullable=False),
        sa.Column("next_path", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("ip_address", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_preauth_login_sessions")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_preauth_login_sessions_token_hash")),
    )
    op.create_index(
        "ix_preauth_login_sessions_expires_at",
        "preauth_login_sessions",
        ["expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_preauth_login_sessions_expires_at", table_name="preauth_login_sessions")
    op.drop_table("preauth_login_sessions")
