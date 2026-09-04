from __future__ import annotations

from collections.abc import Collection

from sqlalchemy.sql.elements import ColumnElement

from shared.models import Ticket


DEFAULT_TICKET_SORT = "updated_desc"
COMMON_TICKET_SORTS = frozenset(
    {
        DEFAULT_TICKET_SORT,
        "updated_asc",
        "created_desc",
        "created_asc",
    }
)


def normalize_ticket_sort(value: str | None, *, allowed: Collection[str]) -> str:
    candidate = (value or "").strip()
    return candidate if candidate in allowed else DEFAULT_TICKET_SORT


def escaped_ilike_pattern(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def common_ticket_order_clauses(sort_key: str) -> tuple[ColumnElement, ...]:
    if sort_key == "updated_desc":
        return Ticket.updated_at.desc(), Ticket.reference_num.desc()
    if sort_key == "updated_asc":
        return Ticket.updated_at.asc(), Ticket.reference_num.asc()
    if sort_key == "created_desc":
        return Ticket.created_at.desc(), Ticket.reference_num.desc()
    if sort_key == "created_asc":
        return Ticket.created_at.asc(), Ticket.reference_num.asc()
    raise ValueError(f"Unsupported common ticket sort: {sort_key}")
