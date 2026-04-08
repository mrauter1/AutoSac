from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select

from shared.agent_specs import LEGACY_PIPELINE_VERSION
from shared.config import Settings
from shared.db import session_scope
from shared.models import AIRun, AIRunStep

ACCEPTED_ANALYSIS_STATUSES = ("succeeded", "human_review")
TERMINAL_RUN_STATUSES = ("skipped", "succeeded", "human_review", "failed", "superseded")


@dataclass(frozen=True)
class AIRunHistoryStatus:
    terminal_runs_missing_pipeline_version: int
    backfilled_legacy_runs_missing_steps: int
    accepted_runs_missing_structured_output: int

    @property
    def is_ready(self) -> bool:
        return (
            self.terminal_runs_missing_pipeline_version == 0
            and self.backfilled_legacy_runs_missing_steps == 0
            and self.accepted_runs_missing_structured_output == 0
        )

    def as_payload(self) -> dict[str, int | bool]:
        return {
            "terminal_runs_missing_pipeline_version": self.terminal_runs_missing_pipeline_version,
            "backfilled_legacy_runs_missing_steps": self.backfilled_legacy_runs_missing_steps,
            "accepted_runs_missing_structured_output": self.accepted_runs_missing_structured_output,
            "is_ready": self.is_ready,
        }

    def error_message(self) -> str:
        problems: list[str] = []
        if self.terminal_runs_missing_pipeline_version:
            problems.append(
                f"{self.terminal_runs_missing_pipeline_version} terminal AI runs are missing pipeline_version"
            )
        if self.backfilled_legacy_runs_missing_steps:
            problems.append(
                f"{self.backfilled_legacy_runs_missing_steps} legacy AI runs are marked backfilled but still have no steps"
            )
        if self.accepted_runs_missing_structured_output:
            problems.append(
                f"{self.accepted_runs_missing_structured_output} accepted AI runs are missing final_output_json"
            )
        if not problems:
            return "AI run history is ready"
        return "AI run history is not ready: " + "; ".join(problems)


def collect_ai_run_history_status(settings: Settings) -> AIRunHistoryStatus:
    step_exists = select(AIRunStep.id).where(AIRunStep.ai_run_id == AIRun.id).exists()
    with session_scope(settings) as db:
        terminal_runs_missing_pipeline_version = int(
            db.execute(
                select(func.count())
                .select_from(AIRun)
                .where(AIRun.status.in_(TERMINAL_RUN_STATUSES), AIRun.pipeline_version.is_(None))
            ).scalar_one()
        )
        backfilled_legacy_runs_missing_steps = int(
            db.execute(
                select(func.count())
                .select_from(AIRun)
                .where(
                    AIRun.status.in_(TERMINAL_RUN_STATUSES),
                    AIRun.pipeline_version == LEGACY_PIPELINE_VERSION,
                    ~step_exists,
                )
            ).scalar_one()
        )
        accepted_runs_missing_structured_output = int(
            db.execute(
                select(func.count())
                .select_from(AIRun)
                .where(
                    AIRun.status.in_(ACCEPTED_ANALYSIS_STATUSES),
                    AIRun.final_output_json.is_(None),
                )
            ).scalar_one()
        )
    return AIRunHistoryStatus(
        terminal_runs_missing_pipeline_version=terminal_runs_missing_pipeline_version,
        backfilled_legacy_runs_missing_steps=backfilled_legacy_runs_missing_steps,
        accepted_runs_missing_structured_output=accepted_runs_missing_structured_output,
    )


def assert_ai_run_history_ready(settings: Settings) -> AIRunHistoryStatus:
    status = collect_ai_run_history_status(settings)
    if not status.is_ready:
        raise RuntimeError(status.error_message())
    return status
