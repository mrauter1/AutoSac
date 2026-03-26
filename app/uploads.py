from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from tempfile import SpooledTemporaryFile
import hashlib
import io
import uuid

from fastapi import Request, UploadFile
from PIL import Image

from shared.config import Settings

MULTIPART_PART_SIZE_SLACK_BYTES = 64 * 1024


class UploadValidationError(ValueError):
    """Raised when an uploaded file violates upload rules."""


@dataclass(frozen=True)
class ValidatedAttachment:
    original_filename: str
    mime_type: str
    sha256: str
    size_bytes: int
    width: int | None
    height: int | None
    data: bytes


async def parse_multipart_form(request: Request, settings: Settings):
    return await request.form(
        max_files=settings.max_images_per_message,
        max_part_size=settings.max_image_bytes + MULTIPART_PART_SIZE_SLACK_BYTES,
    )


def get_form_attachments(form, field_name: str = "attachments") -> list[UploadFile]:
    values = form.getlist(field_name)
    return [
        value
        for value in values
        if hasattr(value, "read") and hasattr(value, "filename") and (getattr(value, "filename", "") or "").strip()
    ]


async def validate_attachment_upload(upload: UploadFile, settings: Settings) -> ValidatedAttachment:
    data = await upload.read()
    if len(data) > settings.max_image_bytes:
        raise UploadValidationError("File exceeds MAX_IMAGE_BYTES")

    width: int | None = None
    height: int | None = None
    try:
        image = Image.open(io.BytesIO(data))
        image.verify()
        reopened = Image.open(io.BytesIO(data))
        width, height = reopened.size
    except Exception:
        width, height = None, None

    return ValidatedAttachment(
        original_filename=upload.filename or "upload",
        mime_type=upload.content_type or "application/octet-stream",
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
        width=width,
        height=height,
        data=data,
    )


def build_attachment_storage_path(settings: Settings, *, ticket_id: uuid.UUID, attachment_id: uuid.UUID, extension: str) -> Path:
    return settings.uploads_dir / str(ticket_id) / f"{attachment_id}{extension}"


def persist_validated_attachment(path: Path, attachment: ValidatedAttachment) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(attachment.data)


def persist_validated_image(path: Path, image: ValidatedAttachment) -> None:
    persist_validated_attachment(path, image)
