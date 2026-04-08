"""Add AI run steps and final structured run output fields.

Revision ID: 20260406_0004
Revises: 20260401_0003
Create Date: 2026-04-06 00:00:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260406_0004"
down_revision = "20260401_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ai_runs", sa.Column("pipeline_version", sa.Text(), nullable=True))
    op.add_column("ai_runs", sa.Column("final_step_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("ai_runs", sa.Column("final_agent_spec_id", sa.Text(), nullable=True))
    op.add_column("ai_runs", sa.Column("final_output_contract", sa.Text(), nullable=True))
    op.add_column("ai_runs", sa.Column("final_output_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True))

    op.create_table(
        "ai_run_steps",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ai_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("step_index", sa.Integer(), nullable=False),
        sa.Column("step_kind", sa.Text(), nullable=False),
        sa.Column("agent_spec_id", sa.Text(), nullable=False),
        sa.Column("agent_spec_version", sa.Text(), nullable=False),
        sa.Column("output_contract", sa.Text(), nullable=False),
        sa.Column("model_name", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("prompt_path", sa.Text(), nullable=True),
        sa.Column("schema_path", sa.Text(), nullable=True),
        sa.Column("final_output_path", sa.Text(), nullable=True),
        sa.Column("stdout_jsonl_path", sa.Text(), nullable=True),
        sa.Column("stderr_path", sa.Text(), nullable=True),
        sa.Column("output_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error_text", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("step_kind IN ('router', 'specialist')", name=op.f("ck_ai_run_steps_ai_run_steps_step_kind")),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'human_review', 'failed', 'skipped', 'superseded')",
            name=op.f("ck_ai_run_steps_ai_run_steps_status"),
        ),
        sa.ForeignKeyConstraint(["ai_run_id"], ["ai_runs.id"], name=op.f("fk_ai_run_steps_ai_run_id_ai_runs")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_run_steps")),
    )
    op.create_index("ix_ai_run_steps_ai_run_id_step_index", "ai_run_steps", ["ai_run_id", "step_index"], unique=False)
    op.create_index("uq_ai_run_steps_ai_run_id_step_index", "ai_run_steps", ["ai_run_id", "step_index"], unique=True)


def downgrade() -> None:
    op.drop_index("uq_ai_run_steps_ai_run_id_step_index", table_name="ai_run_steps")
    op.drop_index("ix_ai_run_steps_ai_run_id_step_index", table_name="ai_run_steps")
    op.drop_table("ai_run_steps")

    op.drop_column("ai_runs", "final_output_json")
    op.drop_column("ai_runs", "final_output_contract")
    op.drop_column("ai_runs", "final_agent_spec_id")
    op.drop_column("ai_runs", "final_step_id")
    op.drop_column("ai_runs", "pipeline_version")
