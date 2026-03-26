from __future__ import annotations

import asyncio
from io import BytesIO
from pathlib import Path

import pytest

UploadFile = pytest.importorskip("starlette.datastructures").UploadFile

from app.uploads import get_form_attachments, validate_attachment_upload
from shared.config import Settings


def _make_settings(tmp_path: Path) -> Settings:
    workspace_dir = tmp_path / "workspace"
    return Settings(
        app_base_url="https://triage.example.test",
        app_secret_key="secret",
        database_url="postgresql+psycopg://triage:triage@localhost:5432/triage",
        uploads_dir=tmp_path / "uploads",
        triage_workspace_dir=workspace_dir,
        repo_mount_dir=workspace_dir / "app",
        manuals_mount_dir=workspace_dir / "manuals",
        codex_bin="codex",
        codex_api_key="key",
        codex_model="",
        codex_timeout_seconds=75,
        worker_poll_seconds=10,
        auto_support_reply_min_confidence=0.85,
        auto_confirm_intent_min_confidence=0.90,
        max_images_per_message=3,
        max_image_bytes=5 * 1024 * 1024,
        session_default_hours=12,
        session_remember_days=30,
    )


class _FakeForm:
    def __init__(self, values):
        self._values = values

    def getlist(self, field_name):
        return self._values if field_name == "attachments" else []


def test_validate_attachment_upload_accepts_non_image(tmp_path):
    upload = UploadFile(filename="notes.pdf", file=BytesIO(b"%PDF-1.4 fake"), headers={"content-type": "application/pdf"})
    validated = asyncio.run(validate_attachment_upload(upload, _make_settings(tmp_path)))

    assert validated.mime_type == "application/pdf"
    assert validated.original_filename == "notes.pdf"
    assert validated.width is None
    assert validated.height is None
    assert validated.size_bytes > 0


def test_validate_attachment_upload_keeps_spoofed_image_mime_but_clears_dimensions(tmp_path):
    upload = UploadFile(filename="fake.png", file=BytesIO(b"not really an image"), headers={"content-type": "image/png"})
    validated = asyncio.run(validate_attachment_upload(upload, _make_settings(tmp_path)))

    assert validated.mime_type == "image/png"
    assert validated.original_filename == "fake.png"
    assert validated.width is None
    assert validated.height is None


def test_get_form_attachments_accepts_starlette_upload_file():
    upload = UploadFile(filename="a.txt", file=BytesIO(b"abc"), headers={"content-type": "text/plain"})
    attachments = get_form_attachments(_FakeForm([upload, "x", None]))
    assert attachments == [upload]
