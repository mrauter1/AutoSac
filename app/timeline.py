from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.models import TicketStatusHistory, User

_HUMAN_ROLE_SUFFIXES = {
    "requester": "requester",
    "dev_ti": "DEV/TI",
}


def load_users_by_ids(db: Session, user_ids: Iterable[uuid.UUID | None]) -> dict[uuid.UUID, User]:
    resolved_ids = tuple({user_id for user_id in user_ids if user_id is not None})
    if not resolved_ids:
        return {}
    return {
        user.id: user
        for user in db.execute(select(User).where(User.id.in_(resolved_ids))).scalars()
    }


def build_author_label(
    *,
    author_type: str,
    display_name: str | None,
    fallback_label: Callable[[str], str],
    role_suffix_label: Callable[[str], str] | None = None,
) -> str:
    suffix = role_suffix_label(author_type) if role_suffix_label is not None and author_type in _HUMAN_ROLE_SUFFIXES else _HUMAN_ROLE_SUFFIXES.get(author_type)
    if suffix is None:
        return fallback_label(author_type)
    return f"{display_name or fallback_label(author_type)} ({suffix})"


def load_ticket_status_history(db: Session, *, ticket_id) -> list[TicketStatusHistory]:
    return list(
        db.execute(
            select(TicketStatusHistory)
            .where(TicketStatusHistory.ticket_id == ticket_id)
            .order_by(TicketStatusHistory.created_at.asc(), TicketStatusHistory.id.asc())
        ).scalars()
    )


def serialize_status_changes(
    history_entries: Iterable[TicketStatusHistory],
    *,
    status_label: Callable[[str], str],
    actor_label: Callable[[str], str],
    actor_role_suffix_label: Callable[[str], str] | None = None,
    status_summary: Callable[[str | None, str], str] | None = None,
    lane_label: str = "Status",
    user_display_names: Mapping[uuid.UUID, str] | None = None,
) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    for entry in history_entries:
        if entry.from_status is None and entry.to_status == "new":
            continue
        changed_by_user_id = getattr(entry, "changed_by_user_id", None)
        from_status_label = status_label(entry.from_status) if entry.from_status else None
        to_status_label = status_label(entry.to_status)
        if status_summary is not None:
            summary = status_summary(from_status_label, to_status_label)
        elif from_status_label:
            summary = f"{from_status_label} -> {to_status_label}"
        else:
            summary = f"Status changed to {to_status_label}"
        items.append(
            {
                "kind": "status_change",
                "id": str(entry.id),
                "created_at": entry.created_at,
                "lane": "status",
                "lane_label": lane_label,
                "actor_label": build_author_label(
                    author_type=entry.changed_by_type,
                    display_name=(user_display_names or {}).get(changed_by_user_id) if changed_by_user_id else None,
                    fallback_label=actor_label,
                    role_suffix_label=actor_role_suffix_label,
                ),
                "from_status_label": from_status_label,
                "to_status_label": to_status_label,
                "summary": summary,
            }
        )
    return items


def merge_timeline_items(*groups: Iterable[dict[str, object]]) -> list[dict[str, object]]:
    flattened: list[tuple[object, int, int, dict[str, object]]] = []
    position = 0
    for group in groups:
        for item in group:
            flattened.append(
                (
                    item["created_at"],
                    0 if item.get("kind") == "message" else 1,
                    position,
                    item,
                )
            )
            position += 1
    flattened.sort(key=lambda entry: entry[:3])
    return [entry[3] for entry in flattened]
