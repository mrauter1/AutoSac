from __future__ import annotations

from worker.step_runner import build_codex_command


class CodexRunError(RuntimeError):
    """Legacy compatibility alias for the removed single-step runner."""


def build_triage_prompt(*args, **kwargs):
    raise CodexRunError("build_triage_prompt was removed; use worker.prompt_renderer.render_agent_prompt")


def prepare_codex_run(*args, **kwargs):
    raise CodexRunError("prepare_codex_run was removed; use worker.step_runner.prepare_step_run")


def execute_codex_run(*args, **kwargs):
    raise CodexRunError("execute_codex_run was removed; use worker.step_runner.execute_step")
