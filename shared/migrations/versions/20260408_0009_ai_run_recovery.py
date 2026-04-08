"""Add AI run worker ownership and stale recovery fields.

Revision ID: 20260408_0009
Revises: 20260407_0008
Create Date: 2026-04-08 00:00:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260408_0009"
down_revision = "20260407_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ai_runs", sa.Column("worker_pid", sa.Integer(), nullable=True))
    op.add_column("ai_runs", sa.Column("worker_instance_id", sa.Text(), nullable=True))
    op.add_column("ai_runs", sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("ai_runs", sa.Column("recovered_from_run_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("ai_runs", sa.Column("recovery_attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False))
    op.create_index("ix_ai_runs_status_last_heartbeat_at", "ai_runs", ["status", "last_heartbeat_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_ai_runs_status_last_heartbeat_at", table_name="ai_runs")
    op.drop_column("ai_runs", "recovery_attempt_count")
    op.drop_column("ai_runs", "recovered_from_run_id")
    op.drop_column("ai_runs", "last_heartbeat_at")
    op.drop_column("ai_runs", "worker_instance_id")
    op.drop_column("ai_runs", "worker_pid")
