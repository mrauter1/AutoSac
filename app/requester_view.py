from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.i18n import (
    DEFAULT_UI_LOCALE,
    requester_author_label,
    requester_role_suffix_label,
    requester_status_change_summary,
    requester_status_label,
    timeline_lane_label,
)
from app.render import render_markdown_to_html
from app.ticket_live import load_ticket_live_state
from app.timeline import (
    build_author_label,
    load_ticket_status_history,
    load_users_by_ids,
    merge_timeline_items,
    serialize_status_changes,
)
from shared.models import Ticket, TicketAttachment, TicketMessage

REQUESTER_INTERACTION_LIVE = "live"
REQUESTER_INTERACTION_ADMIN_PREVIEW = "admin_preview_readonly"
RequesterInteractionMode = Literal["live", "admin_preview_readonly"]


@dataclass(frozen=True)
class RequesterTicketView:
    """The ticket fields that requester-facing templates are allowed to read."""

    reference: str
    title: str
    status: str
    urgent: bool

    @classmethod
    def from_ticket(cls, ticket: Ticket) -> RequesterTicketView:
        return cls(
            reference=ticket.reference,
            title=ticket.title,
            status=ticket.status,
            urgent=ticket.urgent,
        )


@dataclass(frozen=True)
class RequesterAttachmentView:
    """The attachment fields that requester-facing templates are allowed to read."""

    id: Any
    original_filename: str
    mime_type: str
    size_bytes: int

    @classmethod
    def from_attachment(cls, attachment: TicketAttachment) -> RequesterAttachmentView:
        return cls(
            id=attachment.id,
            original_filename=attachment.original_filename,
            mime_type=attachment.mime_type,
            size_bytes=attachment.size_bytes,
        )


def _last_public_message_item_id(timeline: list[dict[str, object]]) -> str | None:
    for item in reversed(timeline):
        if item.get("kind") == "message":
            item_id = item.get("id")
            if isinstance(item_id, str) and item_id:
                return item_id
    return None


def _load_public_ticket_messages(db: Session, *, ticket_id) -> list[TicketMessage]:
    return list(
        db.execute(
            select(TicketMessage)
            .where(TicketMessage.ticket_id == ticket_id, TicketMessage.visibility == "public")
            .order_by(TicketMessage.created_at.asc(), TicketMessage.id.asc())
        ).scalars()
    )


def _load_public_attachments_by_message(
    db: Session,
    *,
    ticket_id,
) -> dict[Any, list[RequesterAttachmentView]]:
    attachments = list(
        db.execute(
            select(TicketAttachment)
            .where(TicketAttachment.ticket_id == ticket_id, TicketAttachment.visibility == "public")
            .order_by(TicketAttachment.created_at.asc(), TicketAttachment.id.asc())
        ).scalars()
    )
    grouped: dict[Any, list[RequesterAttachmentView]] = defaultdict(list)
    for attachment in attachments:
        grouped[attachment.message_id].append(RequesterAttachmentView.from_attachment(attachment))
    return grouped


def serialize_requester_public_thread(
    db: Session,
    *,
    ticket_id,
    ui_locale: str = DEFAULT_UI_LOCALE,
) -> list[dict[str, object]]:
    attachments_by_message = _load_public_attachments_by_message(db, ticket_id=ticket_id)
    messages = _load_public_ticket_messages(db, ticket_id=ticket_id)
    users_by_id = load_users_by_ids(db, (message.author_user_id for message in messages))
    thread: list[dict[str, object]] = []
    for message in messages:
        thread.append(
            {
                "kind": "message",
                "id": str(message.id),
                "created_at": message.created_at,
                "lane": "public",
                "lane_label": timeline_lane_label("public", ui_locale),
                "author_type": message.author_type,
                "author_label": build_author_label(
                    author_type=message.author_type,
                    display_name=(
                        users_by_id.get(message.author_user_id).display_name
                        if message.author_user_id in users_by_id
                        else None
                    ),
                    fallback_label=lambda author_type: requester_author_label(author_type, ui_locale),
                    role_suffix_label=lambda author_type: requester_role_suffix_label(author_type, ui_locale),
                ),
                "source": message.source,
                "body_markdown": message.body_markdown,
                "body_html": render_markdown_to_html(message.body_markdown),
                "attachments": attachments_by_message.get(message.id, []),
            }
        )
    return thread


def build_requester_timeline(
    db: Session,
    *,
    ticket_id,
    ui_locale: str = DEFAULT_UI_LOCALE,
) -> list[dict[str, object]]:
    history_entries = load_ticket_status_history(db, ticket_id=ticket_id)
    users_by_id = load_users_by_ids(db, (getattr(entry, "changed_by_user_id", None) for entry in history_entries))
    return merge_timeline_items(
        serialize_requester_public_thread(db, ticket_id=ticket_id, ui_locale=ui_locale),
        serialize_status_changes(
            history_entries,
            status_label=lambda status: requester_status_label(status, ui_locale),
            actor_label=lambda author_type: requester_author_label(author_type, ui_locale),
            actor_role_suffix_label=lambda author_type: requester_role_suffix_label(author_type, ui_locale),
            status_summary=lambda from_status_label, to_status_label: requester_status_change_summary(
                to_status_label,
                ui_locale,
            ),
            lane_label=timeline_lane_label("status", ui_locale),
            user_display_names={user_id: user.display_name for user_id, user in users_by_id.items()},
        ),
    )


def build_requester_ticket_detail_context(
    db: Session,
    *,
    ticket: Ticket,
    ui_locale: str,
    stale_timeout_seconds: int,
    reply_body: str = "",
    interaction_mode: RequesterInteractionMode = REQUESTER_INTERACTION_LIVE,
) -> dict[str, object]:
    if interaction_mode not in {REQUESTER_INTERACTION_LIVE, REQUESTER_INTERACTION_ADMIN_PREVIEW}:
        raise ValueError(f"Unsupported requester interaction mode: {interaction_mode}")

    timeline = build_requester_timeline(db, ticket_id=ticket.id, ui_locale=ui_locale)
    live_updates_enabled = interaction_mode == REQUESTER_INTERACTION_LIVE
    context: dict[str, object] = {
        "ticket": RequesterTicketView.from_ticket(ticket),
        "timeline": timeline,
        "auto_scroll_message_id": _last_public_message_item_id(timeline),
        "reply_body": reply_body,
        "interaction_mode": interaction_mode,
        "requester_actions_enabled": live_updates_enabled,
        "requester_live_updates_enabled": live_updates_enabled,
    }
    if live_updates_enabled:
        context.update(
            {
                "live_audience": "requester",
                "live_state_url": f"/app/tickets/{ticket.reference}/live-state",
                "live_detail_url": f"/app/tickets/{ticket.reference}",
                "live_state": load_ticket_live_state(
                    db,
                    ticket=ticket,
                    audience="requester",
                    stale_timeout_seconds=stale_timeout_seconds,
                ),
            }
        )
    return context
