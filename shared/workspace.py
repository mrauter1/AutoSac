from __future__ import annotations

from pathlib import Path
import subprocess

from shared.agent_specs import load_all_agent_specs, required_workspace_skill_paths
from shared.config import Settings
from shared.contracts import (
    WORKSPACE_AGENTS_CONTENT,
    WORKSPACE_AGENTS_RELATIVE_PATH,
    WORKSPACE_BOOTSTRAP_VERSION,
)
from shared.routing_registry import load_routing_registry


def _write_exact_file(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8")


def _ensure_git_repo(workspace_dir: Path) -> None:
    if not (workspace_dir / ".git").exists():
        subprocess.run(["git", "init"], cwd=workspace_dir, check=True)

    subprocess.run(["git", "config", "user.name", "Stage 1 Triage Bootstrap"], cwd=workspace_dir, check=True)
    subprocess.run(["git", "config", "user.email", "stage1-triage@example.invalid"], cwd=workspace_dir, check=True)

    head_check = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=workspace_dir,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if head_check.returncode != 0:
        subprocess.run(["git", "commit", "--allow-empty", "-m", "Initial workspace commit"], cwd=workspace_dir, check=True)


def verify_workspace_mounts(settings: Settings) -> None:
    for label, path in (("app", settings.repo_mount_dir), ("manuals", settings.manuals_mount_dir)):
        if not path.exists():
            raise FileNotFoundError(f"Required {label} mount does not exist: {path}")
        if not path.is_dir():
            raise NotADirectoryError(f"Required {label} mount is not a directory: {path}")
        path.stat()


def verify_workspace_contract_paths(settings: Settings) -> None:
    load_routing_registry()
    verify_workspace_mounts(settings)
    for label, path in (
        ("workspace", settings.triage_workspace_dir),
        ("runs", settings.runs_dir),
    ):
        if not path.exists():
            raise FileNotFoundError(f"Required {label} directory does not exist: {path}")
        if not path.is_dir():
            raise NotADirectoryError(f"Required {label} directory is not a directory: {path}")
        path.stat()

    for label, path in (("AGENTS.md", settings.workspace_agents_path),):
        if not path.exists():
            raise FileNotFoundError(f"Required {label} file does not exist: {path}")
        if not path.is_file():
            raise FileNotFoundError(f"Required {label} path is not a file: {path}")
        path.stat()
    for relative_path in required_workspace_skill_paths():
        path = settings.triage_workspace_dir / relative_path
        if not path.exists():
            raise FileNotFoundError(f"Required workspace skill file does not exist: {path}")
        if not path.is_file():
            raise FileNotFoundError(f"Required workspace skill path is not a file: {path}")
        path.stat()


def bootstrap_workspace(settings: Settings) -> None:
    load_routing_registry()
    settings.triage_workspace_dir.mkdir(parents=True, exist_ok=True)
    settings.runs_dir.mkdir(parents=True, exist_ok=True)
    verify_workspace_mounts(settings)
    _write_exact_file(settings.triage_workspace_dir / WORKSPACE_AGENTS_RELATIVE_PATH, WORKSPACE_AGENTS_CONTENT)
    for spec in load_all_agent_specs():
        _write_exact_file(settings.workspace_skill_file_path(spec.skill_id), spec.skill_text)
    _ensure_git_repo(settings.triage_workspace_dir)


def ensure_uploads_dir(settings: Settings) -> None:
    settings.uploads_dir.mkdir(parents=True, exist_ok=True)


def workspace_contract_snapshot(settings: Settings) -> dict[str, str]:
    return {
        "bootstrap_version": WORKSPACE_BOOTSTRAP_VERSION,
        "workspace_dir": str(settings.triage_workspace_dir),
        "repo_mount_dir": str(settings.repo_mount_dir),
        "manuals_mount_dir": str(settings.manuals_mount_dir),
        "agents_path": str(settings.triage_workspace_dir / WORKSPACE_AGENTS_RELATIVE_PATH),
        "skills_dir": str(settings.workspace_skills_dir),
        "skill_ids": ",".join(spec.skill_id for spec in load_all_agent_specs()),
    }
