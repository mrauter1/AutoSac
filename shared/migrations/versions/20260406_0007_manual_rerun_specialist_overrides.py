"""Add manual rerun specialist override storage.

Revision ID: 20260406_0007
Revises: 20260406_0006
Create Date: 2026-04-06 00:00:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260406_0007"
down_revision = "20260406_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tickets", sa.Column("requeue_forced_route_target_id", sa.Text(), nullable=True))
    op.add_column("tickets", sa.Column("requeue_forced_specialist_id", sa.Text(), nullable=True))
    op.add_column("ai_runs", sa.Column("forced_route_target_id", sa.Text(), nullable=True))
    op.add_column("ai_runs", sa.Column("forced_specialist_id", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("ai_runs", "forced_specialist_id")
    op.drop_column("ai_runs", "forced_route_target_id")
    op.drop_column("tickets", "requeue_forced_specialist_id")
    op.drop_column("tickets", "requeue_forced_route_target_id")
