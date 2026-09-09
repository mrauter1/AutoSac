"""Allow canceled tickets.

Revision ID: 20260904_0018
Revises: 20260831_0017
Create Date: 2026-09-04
"""

from alembic import op


revision = "20260904_0018"
down_revision = "20260831_0017"
branch_labels = None
depends_on = None


_PREVIOUS_STATUSES = (
    "new",
    "ai_triage",
    "waiting_on_user",
    "waiting_on_dev_ti",
    "resolved",
)
_CURRENT_STATUSES = (*_PREVIOUS_STATUSES, "canceled")


def _status_constraint(statuses: tuple[str, ...]) -> str:
    values = ", ".join(f"'{status}'" for status in statuses)
    return f"status IN ({values})"


def upgrade() -> None:
    op.drop_constraint(op.f("ck_tickets_tickets_status"), "tickets", type_="check")
    op.create_check_constraint(
        op.f("ck_tickets_tickets_status"),
        "tickets",
        _status_constraint(_CURRENT_STATUSES),
    )


def downgrade() -> None:
    # Preserve rollback viability: the nearest pre-migration terminal state is
    # resolved, and resolved_at must agree with that state.
    op.execute(
        "UPDATE tickets "
        "SET status = 'resolved', resolved_at = COALESCE(resolved_at, updated_at) "
        "WHERE status = 'canceled'"
    )
    op.drop_constraint(op.f("ck_tickets_tickets_status"), "tickets", type_="check")
    op.create_check_constraint(
        op.f("ck_tickets_tickets_status"),
        "tickets",
        _status_constraint(_PREVIOUS_STATUSES),
    )
