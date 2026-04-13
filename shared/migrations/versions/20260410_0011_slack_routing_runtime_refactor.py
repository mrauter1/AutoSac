"""Add Slack routing snapshot and claim token columns.

Revision ID: 20260410_0011
Revises: 20260410_0010
Create Date: 2026-04-10 00:30:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260410_0011"
down_revision = "20260410_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("integration_events", sa.Column("routing_result", sa.Text(), nullable=True))
    op.add_column("integration_events", sa.Column("routing_target_name", sa.Text(), nullable=True))
    op.add_column("integration_events", sa.Column("routing_config_error_code", sa.Text(), nullable=True))
    op.add_column("integration_events", sa.Column("routing_config_error_summary", sa.Text(), nullable=True))
    op.add_column("integration_event_targets", sa.Column("claim_token", postgresql.UUID(as_uuid=True), nullable=True))


def downgrade() -> None:
    op.drop_column("integration_event_targets", "claim_token")
    op.drop_column("integration_events", "routing_config_error_summary")
    op.drop_column("integration_events", "routing_config_error_code")
    op.drop_column("integration_events", "routing_target_name")
    op.drop_column("integration_events", "routing_result")
