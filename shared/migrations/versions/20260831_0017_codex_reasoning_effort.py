"""Record the resolved Codex reasoning effort for AI executions.

Revision ID: 20260831_0017
Revises: 20260827_0016
Create Date: 2026-08-31 00:00:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260831_0017"
down_revision = "20260827_0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ai_runs", sa.Column("reasoning_effort", sa.Text(), nullable=True))
    op.add_column("ai_run_steps", sa.Column("reasoning_effort", sa.Text(), nullable=True))
    op.add_column("codex_turns", sa.Column("reasoning_effort", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("codex_turns", "reasoning_effort")
    op.drop_column("ai_run_steps", "reasoning_effort")
    op.drop_column("ai_runs", "reasoning_effort")
