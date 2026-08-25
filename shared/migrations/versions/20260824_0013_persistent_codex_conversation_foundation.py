"""Add persistent Codex conversation foundation tables and links.

Revision ID: 20260824_0013
Revises: 20260410_0012
Create Date: 2026-08-24 00:00:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260824_0013"
down_revision = "20260410_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "codex_conversations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ticket_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'active'"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('active', 'recovery_required', 'unavailable', 'closed')",
            name="codex_conversations_status",
        ),
        sa.ForeignKeyConstraint(["ticket_id"], ["tickets.id"], name=op.f("fk_codex_conversations_ticket_id_tickets")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_codex_conversations")),
    )
    op.create_index("uq_codex_conversations_ticket_id", "codex_conversations", ["ticket_id"], unique=True)

    op.create_table(
        "codex_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("thread_id", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'active', 'replaced', 'expired', 'deleted')",
            name="codex_sessions_status",
        ),
        sa.CheckConstraint("thread_id IS NULL OR btrim(thread_id) <> ''", name="codex_sessions_thread_id_not_blank"),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["codex_conversations.id"],
            name=op.f("fk_codex_sessions_conversation_id_codex_conversations"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_codex_sessions")),
    )
    op.create_index("uq_codex_sessions_thread_id", "codex_sessions", ["thread_id"], unique=True)
    op.create_index("ix_codex_sessions_conversation_id_created_at", "codex_sessions", ["conversation_id", "created_at"], unique=False)
    op.execute(
        "CREATE UNIQUE INDEX uq_codex_sessions_active_conversation ON codex_sessions (conversation_id) WHERE ended_at IS NULL"
    )

    op.create_table(
        "codex_turns",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("ai_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("turn_index", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'prepared'"), nullable=False),
        sa.Column("specialist_id", sa.Text(), nullable=False),
        sa.Column("agent_spec_version", sa.Text(), nullable=False),
        sa.Column("output_contract", sa.Text(), nullable=False),
        sa.Column("model_name", sa.Text(), nullable=True),
        sa.Column("route_target_id", sa.Text(), nullable=True),
        sa.Column("prompt_path", sa.Text(), nullable=True),
        sa.Column("schema_path", sa.Text(), nullable=True),
        sa.Column("final_output_path", sa.Text(), nullable=True),
        sa.Column("stdout_jsonl_path", sa.Text(), nullable=True),
        sa.Column("stderr_path", sa.Text(), nullable=True),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status IN ('prepared', 'running', 'completed', 'failed', 'interrupted', 'timed_out', 'ambiguous', 'superseded', 'cancelled')",
            name="codex_turns_status",
        ),
        sa.CheckConstraint("turn_index >= 1", name="codex_turns_turn_index_positive"),
        sa.CheckConstraint("specialist_id <> ''", name="codex_turns_specialist_id_not_blank"),
        sa.CheckConstraint("agent_spec_version <> ''", name="codex_turns_agent_spec_version_not_blank"),
        sa.CheckConstraint("output_contract <> ''", name="codex_turns_output_contract_not_blank"),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["codex_conversations.id"],
            name=op.f("fk_codex_turns_conversation_id_codex_conversations"),
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["codex_sessions.id"],
            name=op.f("fk_codex_turns_session_id_codex_sessions"),
        ),
        sa.ForeignKeyConstraint(["ai_run_id"], ["ai_runs.id"], name=op.f("fk_codex_turns_ai_run_id_ai_runs")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_codex_turns")),
    )
    op.create_index("uq_codex_turns_ai_run_id", "codex_turns", ["ai_run_id"], unique=True)
    op.create_index("uq_codex_turns_conversation_id_turn_index", "codex_turns", ["conversation_id", "turn_index"], unique=True)
    op.create_index("ix_codex_turns_session_id_turn_index", "codex_turns", ["session_id", "turn_index"], unique=False)
    op.execute(
        "CREATE UNIQUE INDEX uq_codex_turns_active_conversation ON codex_turns (conversation_id) WHERE status IN ('prepared', 'running')"
    )

    op.create_table(
        "codex_turn_inputs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("turn_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("input_index", sa.Integer(), nullable=False),
        sa.Column("event_kind", sa.Text(), nullable=False),
        sa.Column("source_kind", sa.Text(), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("dedupe_key", sa.Text(), nullable=False),
        sa.Column("payload_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("input_index >= 1", name="codex_turn_inputs_input_index_positive"),
        sa.CheckConstraint(
            "source_kind IN ('ticket', 'ticket_message', 'ticket_status_history', 'ai_draft', 'ai_run', 'ticket_message_publication')",
            name="codex_turn_inputs_source_kind",
        ),
        sa.CheckConstraint("event_kind <> ''", name="codex_turn_inputs_event_kind_not_blank"),
        sa.CheckConstraint("dedupe_key <> ''", name="codex_turn_inputs_dedupe_key_not_blank"),
        sa.ForeignKeyConstraint(["turn_id"], ["codex_turns.id"], name=op.f("fk_codex_turn_inputs_turn_id_codex_turns")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_codex_turn_inputs")),
    )
    op.create_index("uq_codex_turn_inputs_turn_id_input_index", "codex_turn_inputs", ["turn_id", "input_index"], unique=True)
    op.create_index("uq_codex_turn_inputs_turn_id_dedupe_key", "codex_turn_inputs", ["turn_id", "dedupe_key"], unique=True)

    op.create_table(
        "codex_turn_outcomes",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("turn_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("outcome_index", sa.Integer(), nullable=False),
        sa.Column("outcome_kind", sa.Text(), nullable=False),
        sa.Column("payload_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("outcome_index >= 1", name="codex_turn_outcomes_outcome_index_positive"),
        sa.CheckConstraint(
            "outcome_kind IN ('attempted', 'accepted', 'completed', 'auto_published', 'draft_created', 'draft_rejected', 'published_with_edits', 'superseded', 'internal_only_retained', 'failed', 'interrupted', 'timed_out', 'ambiguous')",
            name="codex_turn_outcomes_outcome_kind",
        ),
        sa.ForeignKeyConstraint(["turn_id"], ["codex_turns.id"], name=op.f("fk_codex_turn_outcomes_turn_id_codex_turns")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_codex_turn_outcomes")),
    )
    op.create_index("uq_codex_turn_outcomes_turn_id_outcome_index", "codex_turn_outcomes", ["turn_id", "outcome_index"], unique=True)
    op.create_index("ix_codex_turn_outcomes_turn_id_created_at", "codex_turn_outcomes", ["turn_id", "created_at"], unique=False)

    op.create_table(
        "codex_turn_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("turn_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("item_index", sa.Integer(), nullable=False),
        sa.Column("item_kind", sa.Text(), nullable=False),
        sa.Column("codex_item_id", sa.Text(), nullable=True),
        sa.Column("visibility", sa.Text(), server_default=sa.text("'ops_internal'"), nullable=False),
        sa.Column("payload_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("item_index >= 1", name="codex_turn_items_item_index_positive"),
        sa.CheckConstraint("visibility IN ('ops_internal')", name="codex_turn_items_visibility"),
        sa.CheckConstraint("item_kind <> ''", name="codex_turn_items_item_kind_not_blank"),
        sa.ForeignKeyConstraint(["turn_id"], ["codex_turns.id"], name=op.f("fk_codex_turn_items_turn_id_codex_turns")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_codex_turn_items")),
    )
    op.create_index("uq_codex_turn_items_turn_id_item_index", "codex_turn_items", ["turn_id", "item_index"], unique=True)
    op.create_index("ix_codex_turn_items_turn_id_created_at", "codex_turn_items", ["turn_id", "created_at"], unique=False)

    op.add_column("ticket_messages", sa.Column("codex_turn_outcome_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        op.f("fk_ticket_messages_codex_turn_outcome_id_codex_turn_outcomes"),
        "ticket_messages",
        "codex_turn_outcomes",
        ["codex_turn_outcome_id"],
        ["id"],
    )
    op.create_index("uq_ticket_messages_codex_turn_outcome_id", "ticket_messages", ["codex_turn_outcome_id"], unique=True)
    op.execute(
        "CREATE UNIQUE INDEX uq_ticket_messages_ai_public_ai_run_id "
        "ON ticket_messages (ai_run_id) "
        "WHERE ai_run_id IS NOT NULL AND author_type = 'ai' AND visibility = 'public'"
    )

    op.add_column("ai_drafts", sa.Column("codex_turn_outcome_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        op.f("fk_ai_drafts_codex_turn_outcome_id_codex_turn_outcomes"),
        "ai_drafts",
        "codex_turn_outcomes",
        ["codex_turn_outcome_id"],
        ["id"],
    )
    op.create_index("uq_ai_drafts_codex_turn_outcome_id", "ai_drafts", ["codex_turn_outcome_id"], unique=True)


def downgrade() -> None:
    op.drop_index("uq_ai_drafts_codex_turn_outcome_id", table_name="ai_drafts")
    op.drop_constraint(op.f("fk_ai_drafts_codex_turn_outcome_id_codex_turn_outcomes"), "ai_drafts", type_="foreignkey")
    op.drop_column("ai_drafts", "codex_turn_outcome_id")

    op.execute("DROP INDEX uq_ticket_messages_ai_public_ai_run_id")
    op.drop_index("uq_ticket_messages_codex_turn_outcome_id", table_name="ticket_messages")
    op.drop_constraint(
        op.f("fk_ticket_messages_codex_turn_outcome_id_codex_turn_outcomes"),
        "ticket_messages",
        type_="foreignkey",
    )
    op.drop_column("ticket_messages", "codex_turn_outcome_id")

    op.drop_index("ix_codex_turn_items_turn_id_created_at", table_name="codex_turn_items")
    op.drop_index("uq_codex_turn_items_turn_id_item_index", table_name="codex_turn_items")
    op.drop_table("codex_turn_items")

    op.drop_index("ix_codex_turn_outcomes_turn_id_created_at", table_name="codex_turn_outcomes")
    op.drop_index("uq_codex_turn_outcomes_turn_id_outcome_index", table_name="codex_turn_outcomes")
    op.drop_table("codex_turn_outcomes")

    op.drop_index("uq_codex_turn_inputs_turn_id_dedupe_key", table_name="codex_turn_inputs")
    op.drop_index("uq_codex_turn_inputs_turn_id_input_index", table_name="codex_turn_inputs")
    op.drop_table("codex_turn_inputs")

    op.drop_index("ix_codex_turns_session_id_turn_index", table_name="codex_turns")
    op.drop_index("uq_codex_turns_conversation_id_turn_index", table_name="codex_turns")
    op.drop_index("uq_codex_turns_ai_run_id", table_name="codex_turns")
    op.execute("DROP INDEX uq_codex_turns_active_conversation")
    op.drop_table("codex_turns")

    op.drop_index("ix_codex_sessions_conversation_id_created_at", table_name="codex_sessions")
    op.drop_index("uq_codex_sessions_thread_id", table_name="codex_sessions")
    op.execute("DROP INDEX uq_codex_sessions_active_conversation")
    op.drop_table("codex_sessions")

    op.drop_index("uq_codex_conversations_ticket_id", table_name="codex_conversations")
    op.drop_table("codex_conversations")
