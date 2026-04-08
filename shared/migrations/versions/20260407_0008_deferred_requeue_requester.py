"""Add deferred requeue requester audit storage.

Revision ID: 20260407_0008
Revises: 20260406_0007
Create Date: 2026-04-07 00:00:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260407_0008"
down_revision = "20260406_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tickets",
        sa.Column("requeue_requested_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_tickets_requeue_requested_by_user_id_users",
        "tickets",
        "users",
        ["requeue_requested_by_user_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_tickets_requeue_requested_by_user_id_users", "tickets", type_="foreignkey")
    op.drop_column("tickets", "requeue_requested_by_user_id")
