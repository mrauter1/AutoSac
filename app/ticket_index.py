from __future__ import annotations

from collections.abc import Collection, Iterable

from sqlalchemy.sql.elements import ColumnElement

from shared.models import TICKET_STATUSES, Ticket


DEFAULT_TICKET_SORT = "updated_desc"
COMMON_TICKET_SORTS = frozenset(
    {
        DEFAULT_TICKET_SORT,
        "updated_asc",
        "created_desc",
        "created_asc",
    }
)

TICKET_STATE_GROUPS = (
    "waiting_reply",
    "waiting_user",
    "resolved",
    "canceled",
)
OPS_DEFAULT_TICKET_STATES = ("waiting_reply", "waiting_user")
REQUESTER_DEFAULT_TICKET_STATES = ("waiting_reply", "waiting_user")
TICKET_STATUSES_BY_STATE = {
    "waiting_reply": ("new", "ai_triage", "waiting_on_dev_ti"),
    "waiting_user": ("waiting_on_user",),
    "resolved": ("resolved",),
    "canceled": ("canceled",),
}
_TICKET_STATE_BY_STATUS = {
    status: state
    for state, statuses in TICKET_STATUSES_BY_STATE.items()
    for status in statuses
}

if (
    set(_TICKET_STATE_BY_STATUS) != set(TICKET_STATUSES)
    or len(_TICKET_STATE_BY_STATUS) != sum(map(len, TICKET_STATUSES_BY_STATE.values()))
):
    raise RuntimeError("Ticket state groups must cover every ticket status exactly once")


def ticket_state_for_status(status: str) -> str:
    try:
        return _TICKET_STATE_BY_STATUS[status]
    except KeyError as exc:
        raise ValueError(f"Unsupported ticket status: {status}") from exc


def statuses_for_ticket_states(states: Iterable[str]) -> tuple[str, ...]:
    selected = set(states)
    if selected.difference(TICKET_STATE_GROUPS):
        raise ValueError("Unsupported ticket state")
    return tuple(
        status
        for status in TICKET_STATUSES
        if _TICKET_STATE_BY_STATUS[status] in selected
    )


def normalize_ticket_states(
    values: Iterable[str],
    *,
    default: tuple[str, ...],
) -> tuple[str, ...]:
    candidates = tuple(str(value).strip() for value in values)
    if not candidates or not any(candidates):
        return default
    if any(not candidate for candidate in candidates):
        raise ValueError("Blank ticket states cannot be combined with selected states")
    if len(candidates) > len(TICKET_STATE_GROUPS) or len(set(candidates)) != len(candidates):
        raise ValueError("Ticket states must be unique")
    if any(candidate not in TICKET_STATE_GROUPS for candidate in candidates):
        raise ValueError("Unsupported ticket state")
    selected = set(candidates)
    return tuple(state for state in TICKET_STATE_GROUPS if state in selected)


def ticket_state_query_items(
    states: tuple[str, ...],
    *,
    default: tuple[str, ...],
) -> list[tuple[str, str]]:
    if states == default:
        return []
    return [("state", state) for state in states]


def split_query_items(
    items: Iterable[tuple[str, str]],
    *,
    allowed_keys: Collection[str],
    repeated_keys: Collection[str] = (),
) -> tuple[dict[str, str], dict[str, list[str]]]:
    scalar_values: dict[str, str] = {}
    repeated_values = {key: [] for key in repeated_keys}
    for key, value in items:
        if key not in allowed_keys:
            raise ValueError("Unsupported query parameter")
        if key in repeated_values:
            repeated_values[key].append(value)
        elif key in scalar_values:
            raise ValueError("Duplicate query parameter")
        else:
            scalar_values[key] = value
    return scalar_values, repeated_values


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
