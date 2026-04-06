"""Add route_target_id compatibility storage and selector step persistence.

Revision ID: 20260406_0005
Revises: 20260406_0004
Create Date: 2026-04-06 00:00:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260406_0005"
down_revision = "20260406_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tickets", sa.Column("route_target_id", sa.Text(), nullable=True))
    op.execute("UPDATE tickets SET route_target_id = ticket_class WHERE route_target_id IS NULL AND ticket_class IS NOT NULL")

    op.drop_constraint(op.f("ck_ai_run_steps_ai_run_steps_step_kind"), "ai_run_steps", type_="check")
    op.create_check_constraint(
        op.f("ck_ai_run_steps_ai_run_steps_step_kind"),
        "ai_run_steps",
        "step_kind IN ('router', 'selector', 'specialist')",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_ai_run_steps_ai_run_steps_step_kind"), "ai_run_steps", type_="check")
    op.create_check_constraint(
        op.f("ck_ai_run_steps_ai_run_steps_step_kind"),
        "ai_run_steps",
        "step_kind IN ('router', 'specialist')",
    )

    op.drop_column("tickets", "route_target_id")
