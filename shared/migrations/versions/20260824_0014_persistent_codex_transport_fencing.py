"""Add persistent Codex session lease fields for transport fencing.

Revision ID: 20260824_0014
Revises: 20260824_0013
Create Date: 2026-08-24 00:00:01
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260824_0014"
down_revision = "20260824_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("codex_sessions", sa.Column("lease_owner_run_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("codex_sessions", sa.Column("lease_worker_instance_id", sa.Text(), nullable=True))
    op.add_column("codex_sessions", sa.Column("lease_acquired_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("codex_sessions", sa.Column("lease_heartbeat_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("codex_sessions", sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))

    op.create_foreign_key(
        op.f("fk_codex_sessions_lease_owner_run_id_ai_runs"),
        "codex_sessions",
        "ai_runs",
        ["lease_owner_run_id"],
        ["id"],
    )
    op.create_index("ix_codex_sessions_lease_owner_run_id", "codex_sessions", ["lease_owner_run_id"], unique=False)
    op.create_index("ix_codex_sessions_lease_expires_at", "codex_sessions", ["lease_expires_at"], unique=False)

    op.create_check_constraint(
        "codex_sessions_lease_owner_matches_worker_instance",
        "codex_sessions",
        "(lease_owner_run_id IS NULL) = (lease_worker_instance_id IS NULL)",
    )
    op.create_check_constraint(
        "codex_sessions_lease_owner_matches_acquired_at",
        "codex_sessions",
        "(lease_owner_run_id IS NULL) = (lease_acquired_at IS NULL)",
    )
    op.create_check_constraint(
        "codex_sessions_lease_owner_matches_heartbeat_at",
        "codex_sessions",
        "(lease_owner_run_id IS NULL) = (lease_heartbeat_at IS NULL)",
    )
    op.create_check_constraint(
        "codex_sessions_lease_owner_matches_expires_at",
        "codex_sessions",
        "(lease_owner_run_id IS NULL) = (lease_expires_at IS NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("codex_sessions_lease_owner_matches_expires_at", "codex_sessions", type_="check")
    op.drop_constraint("codex_sessions_lease_owner_matches_heartbeat_at", "codex_sessions", type_="check")
    op.drop_constraint("codex_sessions_lease_owner_matches_acquired_at", "codex_sessions", type_="check")
    op.drop_constraint("codex_sessions_lease_owner_matches_worker_instance", "codex_sessions", type_="check")
    op.drop_index("ix_codex_sessions_lease_expires_at", table_name="codex_sessions")
    op.drop_index("ix_codex_sessions_lease_owner_run_id", table_name="codex_sessions")
    op.drop_constraint(op.f("fk_codex_sessions_lease_owner_run_id_ai_runs"), "codex_sessions", type_="foreignkey")
    op.drop_column("codex_sessions", "lease_expires_at")
    op.drop_column("codex_sessions", "lease_heartbeat_at")
    op.drop_column("codex_sessions", "lease_acquired_at")
    op.drop_column("codex_sessions", "lease_worker_instance_id")
    op.drop_column("codex_sessions", "lease_owner_run_id")
