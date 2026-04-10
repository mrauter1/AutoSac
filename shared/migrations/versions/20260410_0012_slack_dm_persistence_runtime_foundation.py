"""Add Slack DM settings and recipient persistence foundations.

Revision ID: 20260410_0012
Revises: 20260410_0011
Create Date: 2026-04-10 20:20:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260410_0012"
down_revision = "20260410_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DELETE FROM integration_event_targets")
    op.execute("DELETE FROM integration_event_links")
    op.execute("DELETE FROM integration_events")

    op.create_table(
        "slack_dm_settings",
        sa.Column("singleton_key", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("bot_token_ciphertext", sa.Text(), nullable=True),
        sa.Column("team_id", sa.Text(), nullable=True),
        sa.Column("team_name", sa.Text(), nullable=True),
        sa.Column("bot_user_id", sa.Text(), nullable=True),
        sa.Column("validated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notify_ticket_created", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("notify_public_message_added", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("notify_status_changed", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("message_preview_max_chars", sa.Integer(), server_default=sa.text("200"), nullable=False),
        sa.Column("http_timeout_seconds", sa.Integer(), server_default=sa.text("10"), nullable=False),
        sa.Column("delivery_batch_size", sa.Integer(), server_default=sa.text("10"), nullable=False),
        sa.Column("delivery_max_attempts", sa.Integer(), server_default=sa.text("5"), nullable=False),
        sa.Column("delivery_stale_lock_seconds", sa.Integer(), server_default=sa.text("120"), nullable=False),
        sa.Column("updated_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("singleton_key = 'default'", name=op.f("ck_slack_dm_settings_slack_dm_settings_singleton_key")),
        sa.CheckConstraint(
            "message_preview_max_chars >= 4",
            name=op.f("ck_slack_dm_settings_slack_dm_settings_message_preview_max_chars"),
        ),
        sa.CheckConstraint(
            "http_timeout_seconds >= 1 AND http_timeout_seconds <= 30",
            name=op.f("ck_slack_dm_settings_slack_dm_settings_http_timeout_seconds"),
        ),
        sa.CheckConstraint(
            "delivery_batch_size >= 1",
            name=op.f("ck_slack_dm_settings_slack_dm_settings_delivery_batch_size"),
        ),
        sa.CheckConstraint(
            "delivery_max_attempts >= 1",
            name=op.f("ck_slack_dm_settings_slack_dm_settings_delivery_max_attempts"),
        ),
        sa.CheckConstraint(
            "delivery_stale_lock_seconds > http_timeout_seconds",
            name=op.f("ck_slack_dm_settings_slack_dm_settings_delivery_stale_lock_seconds"),
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_user_id"],
            ["users.id"],
            name=op.f("fk_slack_dm_settings_updated_by_user_id_users"),
        ),
        sa.PrimaryKeyConstraint("singleton_key", name=op.f("pk_slack_dm_settings")),
    )

    op.add_column("users", sa.Column("slack_user_id", sa.Text(), nullable=True))
    op.create_unique_constraint(op.f("uq_users_slack_user_id"), "users", ["slack_user_id"])
    op.create_check_constraint(
        op.f("ck_users_users_slack_user_id_not_blank"),
        "users",
        "slack_user_id IS NULL OR btrim(slack_user_id) <> ''",
    )

    op.add_column("integration_event_targets", sa.Column("recipient_user_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("integration_event_targets", sa.Column("recipient_reason", sa.Text(), nullable=True))
    op.create_foreign_key(
        op.f("fk_integration_event_targets_recipient_user_id_users"),
        "integration_event_targets",
        "users",
        ["recipient_user_id"],
        ["id"],
    )
    op.drop_constraint(
        op.f("ck_integration_event_targets_integration_event_targets_target_kind"),
        "integration_event_targets",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_integration_event_targets_integration_event_targets_target_kind"),
        "integration_event_targets",
        "target_kind IN ('slack_webhook', 'slack_dm')",
    )
    op.create_check_constraint(
        op.f("ck_integration_event_targets_integration_event_targets_recipient_reason"),
        "integration_event_targets",
        "recipient_reason IS NULL OR recipient_reason IN ('requester', 'assignee', 'requester_assignee')",
    )
    op.create_check_constraint(
        op.f("ck_integration_event_targets_integration_event_targets_recipient_user_id_matches_target_kind"),
        "integration_event_targets",
        "(target_kind = 'slack_dm') = (recipient_user_id IS NOT NULL)",
    )
    op.create_check_constraint(
        op.f("ck_integration_event_targets_integration_event_targets_recipient_reason_matches_target_kind"),
        "integration_event_targets",
        "(target_kind = 'slack_dm') = (recipient_reason IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_integration_event_targets_integration_event_targets_recipient_reason_matches_target_kind"),
        "integration_event_targets",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_integration_event_targets_integration_event_targets_recipient_user_id_matches_target_kind"),
        "integration_event_targets",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_integration_event_targets_integration_event_targets_recipient_reason"),
        "integration_event_targets",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_integration_event_targets_integration_event_targets_target_kind"),
        "integration_event_targets",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_integration_event_targets_integration_event_targets_target_kind"),
        "integration_event_targets",
        "target_kind IN ('slack_webhook')",
    )
    op.drop_constraint(
        op.f("fk_integration_event_targets_recipient_user_id_users"),
        "integration_event_targets",
        type_="foreignkey",
    )
    op.drop_column("integration_event_targets", "recipient_reason")
    op.drop_column("integration_event_targets", "recipient_user_id")

    op.drop_constraint(op.f("ck_users_users_slack_user_id_not_blank"), "users", type_="check")
    op.drop_constraint(op.f("uq_users_slack_user_id"), "users", type_="unique")
    op.drop_column("users", "slack_user_id")

    op.drop_table("slack_dm_settings")
