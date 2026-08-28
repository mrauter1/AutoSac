"""Add active-turn steering custody schema.

Revision ID: 20260825_0015
Revises: 20260824_0014
Create Date: 2026-08-25 00:00:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260825_0015"
down_revision = "20260824_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "codex_turns",
        sa.Column("transport_kind", sa.Text(), server_default=sa.text("'exec'"), nullable=False),
    )
    op.add_column("codex_turns", sa.Column("native_turn_id", sa.Text(), nullable=True))
    op.add_column("codex_turns", sa.Column("steering_closed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("codex_turns", sa.Column("effective_input_hash", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_codex_turns_codex_turns_transport_kind"),
        "codex_turns",
        "transport_kind IN ('exec', 'app_server')",
    )
    op.create_check_constraint(
        op.f("ck_codex_turns_codex_turns_native_turn_id_not_blank"),
        "codex_turns",
        "native_turn_id IS NULL OR btrim(native_turn_id) <> ''",
    )
    op.create_check_constraint(
        op.f("ck_codex_turns_codex_turns_effective_input_hash_not_blank"),
        "codex_turns",
        "effective_input_hash IS NULL OR btrim(effective_input_hash) <> ''",
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_codex_turns_session_native_turn_id "
        "ON codex_turns (session_id, native_turn_id) "
        "WHERE session_id IS NOT NULL AND native_turn_id IS NOT NULL"
    )

    op.create_table(
        "codex_turn_steers",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("turn_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_kind", sa.Text(), nullable=False),
        sa.Column("source_kind", sa.Text(), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("dedupe_key", sa.Text(), nullable=False),
        sa.Column("expected_native_turn_id", sa.Text(), nullable=False),
        sa.Column("rpc_request_id", sa.Text(), nullable=True),
        sa.Column("payload_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("payload_hash", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'prepared'"), nullable=False),
        sa.Column("attempted_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_text", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "source_kind IN ('ticket', 'ticket_message', 'ticket_status_history', 'ai_draft', 'ai_run', 'ticket_message_publication')",
            name="codex_turn_steers_source_kind",
        ),
        sa.CheckConstraint(
            "status IN ('prepared', 'sending', 'accepted', 'rejected', 'ambiguous')",
            name="codex_turn_steers_status",
        ),
        sa.CheckConstraint("event_kind <> ''", name="codex_turn_steers_event_kind_not_blank"),
        sa.CheckConstraint("source_id IS NOT NULL", name="codex_turn_steers_source_id_not_null"),
        sa.CheckConstraint("dedupe_key <> ''", name="codex_turn_steers_dedupe_key_not_blank"),
        sa.CheckConstraint(
            "btrim(expected_native_turn_id) <> ''",
            name="codex_turn_steers_expected_native_turn_id_not_blank",
        ),
        sa.CheckConstraint(
            "rpc_request_id IS NULL OR btrim(rpc_request_id) <> ''",
            name="codex_turn_steers_rpc_request_id_not_blank",
        ),
        sa.CheckConstraint("payload_hash <> ''", name="codex_turn_steers_payload_hash_not_blank"),
        sa.ForeignKeyConstraint(["turn_id"], ["codex_turns.id"], name=op.f("fk_codex_turn_steers_turn_id_codex_turns")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_codex_turn_steers")),
    )
    op.create_index(
        "uq_codex_turn_steers_turn_id_dedupe_key",
        "codex_turn_steers",
        ["turn_id", "dedupe_key"],
        unique=True,
    )
    op.create_index(
        "ix_codex_turn_steers_turn_id_status_attempted_at",
        "codex_turn_steers",
        ["turn_id", "status", "attempted_at"],
        unique=False,
    )
    op.create_index(
        "ix_codex_turn_steers_source_kind_source_id",
        "codex_turn_steers",
        ["source_kind", "source_id"],
        unique=False,
    )
    op.create_index("ix_codex_turn_steers_rpc_request_id", "codex_turn_steers", ["rpc_request_id"], unique=False)

    op.add_column("tickets", sa.Column("requeue_source_message_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        op.f("fk_tickets_requeue_source_message_id_ticket_messages"),
        "tickets",
        "ticket_messages",
        ["requeue_source_message_id"],
        ["id"],
    )
    op.create_index(
        "ix_tickets_requeue_source_message_id",
        "tickets",
        ["requeue_source_message_id"],
        unique=False,
    )

    op.drop_constraint(op.f("ck_tickets_tickets_requeue_trigger"), "tickets", type_="check")
    op.create_check_constraint(
        op.f("ck_tickets_tickets_requeue_trigger"),
        "tickets",
        "requeue_trigger IS NULL OR requeue_trigger IN ('requester_reply', 'manual_rerun', 'reopen', 'ticket_content')",
    )
    op.drop_constraint(op.f("ck_ai_runs_ai_runs_triggered_by"), "ai_runs", type_="check")
    op.create_check_constraint(
        op.f("ck_ai_runs_ai_runs_triggered_by"),
        "ai_runs",
        "triggered_by IN ('new_ticket', 'requester_reply', 'manual_rerun', 'reopen', 'ticket_content')",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_ai_runs_ai_runs_triggered_by"), "ai_runs", type_="check")
    op.create_check_constraint(
        op.f("ck_ai_runs_ai_runs_triggered_by"),
        "ai_runs",
        "triggered_by IN ('new_ticket', 'requester_reply', 'manual_rerun', 'reopen')",
    )
    op.drop_constraint(op.f("ck_tickets_tickets_requeue_trigger"), "tickets", type_="check")
    op.create_check_constraint(
        op.f("ck_tickets_tickets_requeue_trigger"),
        "tickets",
        "requeue_trigger IS NULL OR requeue_trigger IN ('requester_reply', 'manual_rerun', 'reopen')",
    )

    op.drop_index("ix_tickets_requeue_source_message_id", table_name="tickets")
    op.drop_constraint(op.f("fk_tickets_requeue_source_message_id_ticket_messages"), "tickets", type_="foreignkey")
    op.drop_column("tickets", "requeue_source_message_id")

    op.drop_index("ix_codex_turn_steers_rpc_request_id", table_name="codex_turn_steers")
    op.drop_index("ix_codex_turn_steers_source_kind_source_id", table_name="codex_turn_steers")
    op.drop_index("ix_codex_turn_steers_turn_id_status_attempted_at", table_name="codex_turn_steers")
    op.drop_index("uq_codex_turn_steers_turn_id_dedupe_key", table_name="codex_turn_steers")
    op.drop_table("codex_turn_steers")

    op.execute("DROP INDEX uq_codex_turns_session_native_turn_id")
    op.drop_constraint(op.f("ck_codex_turns_codex_turns_effective_input_hash_not_blank"), "codex_turns", type_="check")
    op.drop_constraint(op.f("ck_codex_turns_codex_turns_native_turn_id_not_blank"), "codex_turns", type_="check")
    op.drop_constraint(op.f("ck_codex_turns_codex_turns_transport_kind"), "codex_turns", type_="check")
    op.drop_column("codex_turns", "effective_input_hash")
    op.drop_column("codex_turns", "steering_closed_at")
    op.drop_column("codex_turns", "native_turn_id")
    op.drop_column("codex_turns", "transport_kind")
