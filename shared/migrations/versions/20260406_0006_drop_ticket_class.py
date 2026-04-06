"""Drop legacy ticket_class storage after route-target cutover.

Revision ID: 20260406_0006
Revises: 20260406_0005
Create Date: 2026-04-06 00:00:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260406_0006"
down_revision = "20260406_0005"
branch_labels = None
depends_on = None

LEGACY_TICKET_CLASS_IDS = ("support", "access_config", "data_ops", "bug", "feature", "unknown")


def _legacy_ticket_class_constraint_sql() -> str:
    quoted = ", ".join(f"'{value}'" for value in LEGACY_TICKET_CLASS_IDS)
    return f"ticket_class IS NULL OR ticket_class IN ({quoted})"


def upgrade() -> None:
    with op.batch_alter_table("tickets") as batch_op:
        batch_op.drop_constraint(op.f("ck_tickets_tickets_ticket_class"), type_="check")
        batch_op.drop_column("ticket_class")


def downgrade() -> None:
    with op.batch_alter_table("tickets") as batch_op:
        batch_op.add_column(sa.Column("ticket_class", sa.Text(), nullable=True))
    op.execute(
        """
        UPDATE tickets
        SET ticket_class = route_target_id
        WHERE ticket_class IS NULL
          AND route_target_id IN ('support', 'access_config', 'data_ops', 'bug', 'feature', 'unknown')
        """
    )
    with op.batch_alter_table("tickets") as batch_op:
        batch_op.create_check_constraint(
            op.f("ck_tickets_tickets_ticket_class"),
            _legacy_ticket_class_constraint_sql(),
        )
