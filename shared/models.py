from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Sequence,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from shared.db import Base
from shared.security import utc_now

USER_ROLES = ("requester", "dev_ti", "admin")
TICKET_STATUSES = ("new", "ai_triage", "waiting_on_user", "waiting_on_dev_ti", "resolved")
IMPACT_LEVELS = ("low", "medium", "high", "unknown")
AUTHOR_TYPES = ("requester", "dev_ti", "ai", "system")
VISIBILITIES = ("public", "internal")
MESSAGE_SOURCES = (
    "ticket_create",
    "requester_reply",
    "human_public_reply",
    "human_internal_note",
    "ai_auto_public",
    "ai_internal_note",
    "ai_draft_published",
    "system",
)
AI_RUN_STATUSES = ("pending", "running", "succeeded", "human_review", "failed", "skipped", "superseded")
AI_RUN_TRIGGERS = ("new_ticket", "requester_reply", "manual_rerun", "reopen")
AI_RUN_STEP_KINDS = ("router", "selector", "specialist")
AI_RUN_STEP_STATUSES = AI_RUN_STATUSES
REQUEUE_TRIGGERS = ("requester_reply", "manual_rerun", "reopen")
AI_DRAFT_KINDS = ("public_reply",)
AI_DRAFT_STATUSES = ("pending_approval", "approved", "rejected", "superseded", "published")
INTEGRATION_EVENT_TYPES = ("ticket.created", "ticket.public_message_added", "ticket.status_changed")
INTEGRATION_AGGREGATE_TYPES = ("ticket",)
INTEGRATION_EVENT_LINK_ENTITY_TYPES = ("ticket", "ticket_message", "ticket_status_history")
INTEGRATION_EVENT_LINK_RELATION_KINDS = ("primary", "message", "status_history")
INTEGRATION_TARGET_KINDS = ("slack_webhook", "slack_dm")
SLACK_RECIPIENT_REASONS = ("requester", "assignee", "requester_assignee")
INTEGRATION_DELIVERY_STATUSES = ("pending", "processing", "sent", "failed", "dead_letter")

TICKET_REFERENCE_NUM_SEQUENCE = Sequence("ticket_reference_num_seq")


def _enum_sql(values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{value}'" for value in values)
    return f"({quoted})"


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(f"role IN {_enum_sql(USER_ROLES)}", name="users_role"),
        CheckConstraint("slack_user_id IS NULL OR btrim(slack_user_id) <> ''", name="users_slack_user_id_not_blank"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    slack_user_id: Mapped[str | None] = mapped_column(Text, nullable=True, unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now, server_default=text("now()"))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SessionRecord(Base):
    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    csrf_token: Mapped[str] = mapped_column(Text, nullable=False)
    remember_me: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, server_default=text("now()"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, server_default=text("now()"))
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip_address: Mapped[str | None] = mapped_column(Text, nullable=True)


class PreauthLoginSession(Base):
    __tablename__ = "preauth_login_sessions"
    __table_args__ = (
        Index("ix_preauth_login_sessions_expires_at", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    csrf_token: Mapped[str] = mapped_column(Text, nullable=False)
    next_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, server_default=text("now()"))
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip_address: Mapped[str | None] = mapped_column(Text, nullable=True)


class Ticket(Base):
    __tablename__ = "tickets"
    __table_args__ = (
        CheckConstraint(f"status IN {_enum_sql(TICKET_STATUSES)}", name="tickets_status"),
        CheckConstraint(f"impact_level IS NULL OR impact_level IN {_enum_sql(IMPACT_LEVELS)}", name="tickets_impact_level"),
        CheckConstraint(f"requeue_trigger IS NULL OR requeue_trigger IN {_enum_sql(REQUEUE_TRIGGERS)}", name="tickets_requeue_trigger"),
        Index("ix_tickets_status_updated_at", "status", text("updated_at DESC")),
        Index("ix_tickets_created_by_user_id_updated_at", "created_by_user_id", text("updated_at DESC")),
        Index("ix_tickets_assigned_to_user_id_updated_at", "assigned_to_user_id", text("updated_at DESC")),
        Index("ix_tickets_urgent_status_updated_at", "urgent", "status", text("updated_at DESC")),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    reference_num: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        unique=True,
        server_default=text("nextval('ticket_reference_num_seq')"),
    )
    reference: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    assigned_to_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    urgent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    route_target_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_confidence: Mapped[float | None] = mapped_column(Numeric(4, 3), nullable=True)
    impact_level: Mapped[str | None] = mapped_column(Text, nullable=True)
    development_needed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    clarification_rounds: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    requester_language: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_processed_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_ai_action: Mapped[str | None] = mapped_column(Text, nullable=True)
    requeue_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    requeue_trigger: Mapped[str | None] = mapped_column(Text, nullable=True)
    requeue_requested_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    requeue_forced_route_target_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    requeue_forced_specialist_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now, server_default=text("now()"))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TicketMessage(Base):
    __tablename__ = "ticket_messages"
    __table_args__ = (
        CheckConstraint(f"author_type IN {_enum_sql(AUTHOR_TYPES)}", name="ticket_messages_author_type"),
        CheckConstraint(f"visibility IN {_enum_sql(VISIBILITIES)}", name="ticket_messages_visibility"),
        CheckConstraint(f"source IN {_enum_sql(MESSAGE_SOURCES)}", name="ticket_messages_source"),
        Index("ix_ticket_messages_ticket_id_created_at", "ticket_id", "created_at"),
        Index("ix_ticket_messages_ticket_id_visibility_created_at", "ticket_id", "visibility", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ticket_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tickets.id"), nullable=False)
    author_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    author_type: Mapped[str] = mapped_column(Text, nullable=False)
    visibility: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    body_markdown: Mapped[str] = mapped_column(Text, nullable=False)
    body_text: Mapped[str] = mapped_column(Text, nullable=False)
    ai_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ai_runs.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, server_default=text("now()"))


class TicketAttachment(Base):
    __tablename__ = "ticket_attachments"
    __table_args__ = (
        CheckConstraint(f"visibility IN {_enum_sql(VISIBILITIES)}", name="ticket_attachments_visibility"),
        Index("ix_ticket_attachments_ticket_id", "ticket_id"),
        Index("ix_ticket_attachments_message_id", "message_id"),
        Index("ix_ticket_attachments_sha256", "sha256"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ticket_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tickets.id"), nullable=False)
    message_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ticket_messages.id"), nullable=False)
    visibility: Mapped[str] = mapped_column(Text, nullable=False)
    original_filename: Mapped[str] = mapped_column(Text, nullable=False)
    stored_path: Mapped[str] = mapped_column(Text, nullable=False)
    mime_type: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, server_default=text("now()"))


class TicketStatusHistory(Base):
    __tablename__ = "ticket_status_history"
    __table_args__ = (
        CheckConstraint(f"changed_by_type IN {_enum_sql(AUTHOR_TYPES)}", name="ticket_status_history_changed_by_type"),
        Index("ix_ticket_status_history_ticket_id_created_at", "ticket_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ticket_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tickets.id"), nullable=False)
    from_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    to_status: Mapped[str] = mapped_column(Text, nullable=False)
    changed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    changed_by_type: Mapped[str] = mapped_column(Text, nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, server_default=text("now()"))


class TicketView(Base):
    __tablename__ = "ticket_views"

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), primary_key=True)
    ticket_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tickets.id"), primary_key=True)
    last_viewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, server_default=text("now()"))


class IntegrationEvent(Base):
    __tablename__ = "integration_events"
    __table_args__ = (
        CheckConstraint(f"event_type IN {_enum_sql(INTEGRATION_EVENT_TYPES)}", name="integration_events_event_type"),
        CheckConstraint(f"aggregate_type IN {_enum_sql(INTEGRATION_AGGREGATE_TYPES)}", name="integration_events_aggregate_type"),
        Index("ix_integration_events_event_type_created_at", "event_type", "created_at"),
        Index("ix_integration_events_aggregate_type_aggregate_id_created_at", "aggregate_type", "aggregate_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_system: Mapped[str] = mapped_column(Text, nullable=False, default="autosac", server_default=text("'autosac'"))
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    aggregate_type: Mapped[str] = mapped_column(Text, nullable=False)
    aggregate_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    dedupe_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    payload_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    routing_result: Mapped[str | None] = mapped_column(Text, nullable=True)
    routing_target_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    routing_config_error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    routing_config_error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, server_default=text("now()"))


class IntegrationEventLink(Base):
    __tablename__ = "integration_event_links"
    __table_args__ = (
        CheckConstraint(
            f"entity_type IN {_enum_sql(INTEGRATION_EVENT_LINK_ENTITY_TYPES)}",
            name="integration_event_links_entity_type",
        ),
        CheckConstraint(
            f"relation_kind IN {_enum_sql(INTEGRATION_EVENT_LINK_RELATION_KINDS)}",
            name="integration_event_links_relation_kind",
        ),
        Index(
            "uq_integration_event_links_event_id_entity_type_entity_id_relation_kind",
            "event_id",
            "entity_type",
            "entity_id",
            "relation_kind",
            unique=True,
        ),
        Index("ix_integration_event_links_event_id", "event_id"),
        Index("ix_integration_event_links_entity_type_entity_id", "entity_type", "entity_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("integration_events.id"), nullable=False)
    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    relation_kind: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, server_default=text("now()"))


class IntegrationEventTarget(Base):
    __tablename__ = "integration_event_targets"
    __table_args__ = (
        CheckConstraint(f"target_kind IN {_enum_sql(INTEGRATION_TARGET_KINDS)}", name="integration_event_targets_target_kind"),
        CheckConstraint(
            f"recipient_reason IS NULL OR recipient_reason IN {_enum_sql(SLACK_RECIPIENT_REASONS)}",
            name="integration_event_targets_recipient_reason",
        ),
        CheckConstraint(
            "(target_kind = 'slack_dm') = (recipient_user_id IS NOT NULL)",
            name="integration_event_targets_recipient_user_id_matches_target_kind",
        ),
        CheckConstraint(
            "(target_kind = 'slack_dm') = (recipient_reason IS NOT NULL)",
            name="integration_event_targets_recipient_reason_matches_target_kind",
        ),
        CheckConstraint(
            f"delivery_status IN {_enum_sql(INTEGRATION_DELIVERY_STATUSES)}",
            name="integration_event_targets_delivery_status",
        ),
        CheckConstraint("attempt_count >= 0", name="integration_event_targets_attempt_count_non_negative"),
        CheckConstraint("(delivery_status = 'sent') = (sent_at IS NOT NULL)", name="integration_event_targets_sent_at_matches_status"),
        CheckConstraint(
            "(delivery_status = 'dead_letter') = (dead_lettered_at IS NOT NULL)",
            name="integration_event_targets_dead_lettered_at_matches_status",
        ),
        CheckConstraint(
            "NOT (sent_at IS NOT NULL AND dead_lettered_at IS NOT NULL)",
            name="integration_event_targets_terminal_timestamps",
        ),
        Index("uq_integration_event_targets_event_id_target_name", "event_id", "target_name", unique=True),
        Index("ix_integration_event_targets_delivery_status_next_attempt_at", "delivery_status", "next_attempt_at"),
        Index("ix_integration_event_targets_locked_at", "locked_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("integration_events.id"), nullable=False)
    target_name: Mapped[str] = mapped_column(Text, nullable=False)
    target_kind: Mapped[str] = mapped_column(Text, nullable=False)
    recipient_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    recipient_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    delivery_status: Mapped[str] = mapped_column(Text, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, server_default=text("now()"))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    claim_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dead_lettered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, server_default=text("now()"))


class SlackDMSettings(Base):
    __tablename__ = "slack_dm_settings"
    __table_args__ = (
        CheckConstraint("singleton_key = 'default'", name="slack_dm_settings_singleton_key"),
        CheckConstraint("message_preview_max_chars >= 4", name="slack_dm_settings_message_preview_max_chars"),
        CheckConstraint(
            "http_timeout_seconds >= 1 AND http_timeout_seconds <= 30",
            name="slack_dm_settings_http_timeout_seconds",
        ),
        CheckConstraint("delivery_batch_size >= 1", name="slack_dm_settings_delivery_batch_size"),
        CheckConstraint("delivery_max_attempts >= 1", name="slack_dm_settings_delivery_max_attempts"),
        CheckConstraint(
            "delivery_stale_lock_seconds > http_timeout_seconds",
            name="slack_dm_settings_delivery_stale_lock_seconds",
        ),
    )

    singleton_key: Mapped[str] = mapped_column(Text, primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    bot_token_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    team_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    team_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    bot_user_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notify_ticket_created: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    notify_public_message_added: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    notify_status_changed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    message_preview_max_chars: Mapped[int] = mapped_column(Integer, nullable=False, default=200, server_default=text("200"))
    http_timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=10, server_default=text("10"))
    delivery_batch_size: Mapped[int] = mapped_column(Integer, nullable=False, default=10, server_default=text("10"))
    delivery_max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5, server_default=text("5"))
    delivery_stale_lock_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=120, server_default=text("120"))
    updated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
        server_default=text("now()"),
    )


class AIRun(Base):
    __tablename__ = "ai_runs"
    __table_args__ = (
        CheckConstraint(f"status IN {_enum_sql(AI_RUN_STATUSES)}", name="ai_runs_status"),
        CheckConstraint(f"triggered_by IN {_enum_sql(AI_RUN_TRIGGERS)}", name="ai_runs_triggered_by"),
        Index("ix_ai_runs_status_created_at", "status", "created_at"),
        Index("ix_ai_runs_status_last_heartbeat_at", "status", "last_heartbeat_at"),
        Index("ix_ai_runs_ticket_id_created_at_desc", "ticket_id", text("created_at DESC")),
        Index(
            "uq_ai_runs_active_ticket",
            "ticket_id",
            unique=True,
            postgresql_where=text("status IN ('pending', 'running')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ticket_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tickets.id"), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    triggered_by: Mapped[str] = mapped_column(Text, nullable=False)
    requested_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    input_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    model_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    pipeline_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    forced_route_target_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    forced_specialist_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    final_step_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    final_agent_spec_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    final_output_contract: Mapped[str | None] = mapped_column(Text, nullable=True)
    final_output_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    prompt_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    schema_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    final_output_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    stdout_jsonl_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    stderr_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    worker_pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    worker_instance_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    recovered_from_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    recovery_attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    error_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, server_default=text("now()"))


class AIRunStep(Base):
    __tablename__ = "ai_run_steps"
    __table_args__ = (
        CheckConstraint(f"step_kind IN {_enum_sql(AI_RUN_STEP_KINDS)}", name="ai_run_steps_step_kind"),
        CheckConstraint(f"status IN {_enum_sql(AI_RUN_STEP_STATUSES)}", name="ai_run_steps_status"),
        Index("ix_ai_run_steps_ai_run_id_step_index", "ai_run_id", "step_index"),
        Index("uq_ai_run_steps_ai_run_id_step_index", "ai_run_id", "step_index", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ai_run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ai_runs.id"), nullable=False)
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    step_kind: Mapped[str] = mapped_column(Text, nullable=False)
    agent_spec_id: Mapped[str] = mapped_column(Text, nullable=False)
    agent_spec_version: Mapped[str] = mapped_column(Text, nullable=False)
    output_contract: Mapped[str] = mapped_column(Text, nullable=False)
    model_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    schema_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    final_output_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    stdout_jsonl_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    stderr_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    output_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, server_default=text("now()"))


class AIDraft(Base):
    __tablename__ = "ai_drafts"
    __table_args__ = (
        CheckConstraint(f"kind IN {_enum_sql(AI_DRAFT_KINDS)}", name="ai_drafts_kind"),
        CheckConstraint(f"status IN {_enum_sql(AI_DRAFT_STATUSES)}", name="ai_drafts_status"),
        Index("ix_ai_drafts_ticket_id_status_created_at_desc", "ticket_id", "status", text("created_at DESC")),
        Index("ix_ai_drafts_ai_run_id", "ai_run_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ticket_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tickets.id"), nullable=False)
    ai_run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ai_runs.id"), nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    body_markdown: Mapped[str] = mapped_column(Text, nullable=False)
    body_text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, server_default=text("now()"))
    reviewed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_message_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ticket_messages.id"), nullable=True)


class SystemState(Base):
    __tablename__ = "system_state"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now, server_default=text("now()"))
