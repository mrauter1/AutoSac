from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_architecture_entry_points_describe_current_active_turn_rollout() -> None:
    readme = _read("architecture/README.md")
    traceability = _read("architecture/source-traceability.md")
    atlas = _read("architecture/index.html")

    for content in (readme, traceability, atlas):
        assert "current working tree" in content
        assert "app-server" in content

    assert "Codex active-turn steering plan" in readme
    assert "worker/codex_app_server.py" in traceability
    assert "Router and selector" in traceability
    assert "22af53a</code>" not in atlas
    assert "must not be read as current behavior" not in readme


def test_architecture_diagrams_include_codex_active_turn_custody() -> None:
    runtime = _read("architecture/diagrams/runtime-containers.mmd")
    pipeline = _read("architecture/diagrams/ai-pipeline.mmd")
    data_model = _read("architecture/diagrams/data-model.mmd")

    assert "App-server stdio client" in runtime
    assert "Run-scoped app-server stdio" in pipeline
    assert "Strict unseen steer receipts" in pipeline
    assert "CODEX_TURN_STEER" in data_model
