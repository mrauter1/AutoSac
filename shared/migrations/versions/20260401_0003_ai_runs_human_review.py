"""Add human_review AI run status.

Revision ID: 20260401_0003
Revises: 20260324_0002
Create Date: 2026-04-01 00:00:00
"""
from __future__ import annotations

from alembic import op

revision = "20260401_0003"
down_revision = "20260324_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(op.f("ck_ai_runs_ai_runs_status"), "ai_runs", type_="check")
    op.create_check_constraint(
        op.f("ck_ai_runs_ai_runs_status"),
        "ai_runs",
        "status IN ('pending', 'running', 'succeeded', 'human_review', 'failed', 'skipped', 'superseded')",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_ai_runs_ai_runs_status"), "ai_runs", type_="check")
    op.create_check_constraint(
        op.f("ck_ai_runs_ai_runs_status"),
        "ai_runs",
        "status IN ('pending', 'running', 'succeeded', 'failed', 'skipped', 'superseded')",
    )
