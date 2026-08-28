"""Record published AI internal notes as causal Codex outcomes.

Revision ID: 20260827_0016
Revises: 20260825_0015
Create Date: 2026-08-27 00:00:00
"""
from __future__ import annotations

from alembic import op


revision = "20260827_0016"
down_revision = "20260825_0015"
branch_labels = None
depends_on = None


_OUTCOME_KINDS_WITH_INTERNAL_NOTE = (
    "attempted",
    "accepted",
    "completed",
    "auto_published",
    "draft_created",
    "draft_rejected",
    "published_with_edits",
    "internal_note_published",
    "superseded",
    "internal_only_retained",
    "failed",
    "interrupted",
    "timed_out",
    "ambiguous",
)


def _outcome_kind_check(values: tuple[str, ...]) -> str:
    return "outcome_kind IN (" + ", ".join(f"'{value}'" for value in values) + ")"


def upgrade() -> None:
    op.drop_constraint(
        op.f("ck_codex_turn_outcomes_codex_turn_outcomes_outcome_kind"),
        "codex_turn_outcomes",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_codex_turn_outcomes_codex_turn_outcomes_outcome_kind"),
        "codex_turn_outcomes",
        _outcome_kind_check(_OUTCOME_KINDS_WITH_INTERNAL_NOTE),
    )


def downgrade() -> None:
    op.execute(
        "UPDATE ticket_messages SET codex_turn_outcome_id = NULL "
        "WHERE codex_turn_outcome_id IN ("
        "SELECT id FROM codex_turn_outcomes WHERE outcome_kind = 'internal_note_published'"
        ")"
    )
    op.execute("DELETE FROM codex_turn_outcomes WHERE outcome_kind = 'internal_note_published'")
    op.drop_constraint(
        op.f("ck_codex_turn_outcomes_codex_turn_outcomes_outcome_kind"),
        "codex_turn_outcomes",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_codex_turn_outcomes_codex_turn_outcomes_outcome_kind"),
        "codex_turn_outcomes",
        _outcome_kind_check(
            tuple(value for value in _OUTCOME_KINDS_WITH_INTERNAL_NOTE if value != "internal_note_published")
        ),
    )
