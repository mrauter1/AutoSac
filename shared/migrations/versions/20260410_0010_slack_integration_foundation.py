"""Add Slack integration persistence foundation.

Revision ID: 20260410_0010
Revises: 20260408_0009
Create Date: 2026-04-10 00:00:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260410_0010"
down_revision = "20260408_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "integration_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_system", sa.Text(), server_default=sa.text("'autosac'"), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("aggregate_type", sa.Text(), nullable=False),
        sa.Column("aggregate_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("dedupe_key", sa.Text(), nullable=False),
        sa.Column("payload_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "event_type IN ('ticket.created', 'ticket.public_message_added', 'ticket.status_changed')",
            name=op.f("ck_integration_events_integration_events_event_type"),
        ),
        sa.CheckConstraint(
            "aggregate_type IN ('ticket')",
            name=op.f("ck_integration_events_integration_events_aggregate_type"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_integration_events")),
        sa.UniqueConstraint("dedupe_key", name=op.f("uq_integration_events_dedupe_key")),
    )
    op.create_index("ix_integration_events_event_type_created_at", "integration_events", ["event_type", "created_at"], unique=False)
    op.create_index(
        "ix_integration_events_aggregate_type_aggregate_id_created_at",
        "integration_events",
        ["aggregate_type", "aggregate_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "integration_event_links",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("entity_type", sa.Text(), nullable=False),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("relation_kind", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "entity_type IN ('ticket', 'ticket_message', 'ticket_status_history')",
            name=op.f("ck_integration_event_links_integration_event_links_entity_type"),
        ),
        sa.CheckConstraint(
            "relation_kind IN ('primary', 'message', 'status_history')",
            name=op.f("ck_integration_event_links_integration_event_links_relation_kind"),
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["integration_events.id"],
            name=op.f("fk_integration_event_links_event_id_integration_events"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_integration_event_links")),
    )
    op.create_index(
        "uq_integration_event_links_event_id_entity_type_entity_id_relation_kind",
        "integration_event_links",
        ["event_id", "entity_type", "entity_id", "relation_kind"],
        unique=True,
    )
    op.create_index("ix_integration_event_links_event_id", "integration_event_links", ["event_id"], unique=False)
    op.create_index(
        "ix_integration_event_links_entity_type_entity_id",
        "integration_event_links",
        ["entity_type", "entity_id"],
        unique=False,
    )

    op.create_table(
        "integration_event_targets",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_name", sa.Text(), nullable=False),
        sa.Column("target_kind", sa.Text(), nullable=False),
        sa.Column("delivery_status", sa.Text(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_by", sa.Text(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dead_lettered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "target_kind IN ('slack_webhook')",
            name=op.f("ck_integration_event_targets_integration_event_targets_target_kind"),
        ),
        sa.CheckConstraint(
            "delivery_status IN ('pending', 'processing', 'sent', 'failed', 'dead_letter')",
            name=op.f("ck_integration_event_targets_integration_event_targets_delivery_status"),
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name=op.f("ck_integration_event_targets_integration_event_targets_attempt_count_non_negative"),
        ),
        sa.CheckConstraint(
            "(delivery_status = 'sent') = (sent_at IS NOT NULL)",
            name=op.f("ck_integration_event_targets_integration_event_targets_sent_at_matches_status"),
        ),
        sa.CheckConstraint(
            "(delivery_status = 'dead_letter') = (dead_lettered_at IS NOT NULL)",
            name=op.f("ck_integration_event_targets_integration_event_targets_dead_lettered_at_matches_status"),
        ),
        sa.CheckConstraint(
            "NOT (sent_at IS NOT NULL AND dead_lettered_at IS NOT NULL)",
            name=op.f("ck_integration_event_targets_integration_event_targets_terminal_timestamps"),
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["integration_events.id"],
            name=op.f("fk_integration_event_targets_event_id_integration_events"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_integration_event_targets")),
    )
    op.create_index(
        "uq_integration_event_targets_event_id_target_name",
        "integration_event_targets",
        ["event_id", "target_name"],
        unique=True,
    )
    op.create_index(
        "ix_integration_event_targets_delivery_status_next_attempt_at",
        "integration_event_targets",
        ["delivery_status", "next_attempt_at"],
        unique=False,
    )
    op.create_index("ix_integration_event_targets_locked_at", "integration_event_targets", ["locked_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_integration_event_targets_locked_at", table_name="integration_event_targets")
    op.drop_index("ix_integration_event_targets_delivery_status_next_attempt_at", table_name="integration_event_targets")
    op.drop_index("uq_integration_event_targets_event_id_target_name", table_name="integration_event_targets")
    op.drop_table("integration_event_targets")

    op.drop_index("ix_integration_event_links_entity_type_entity_id", table_name="integration_event_links")
    op.drop_index("ix_integration_event_links_event_id", table_name="integration_event_links")
    op.drop_index("uq_integration_event_links_event_id_entity_type_entity_id_relation_kind", table_name="integration_event_links")
    op.drop_table("integration_event_links")

    op.drop_index("ix_integration_events_aggregate_type_aggregate_id_created_at", table_name="integration_events")
    op.drop_index("ix_integration_events_event_type_created_at", table_name="integration_events")
    op.drop_table("integration_events")
