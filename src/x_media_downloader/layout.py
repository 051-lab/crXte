from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from .models import Analysis, Attachment, MediaType, PostMetadata, QualityOption

_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
_INVALID_COMPONENT = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class LayoutError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ExportLayout:
    root: Path
    output_dir: Path
    media_dir: Path
    markdown_path: Path
    pdf_path: Path


def sanitize_component(value: str, fallback: str, max_length: int = 80) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip()
    cleaned = _INVALID_COMPONENT.sub("_", normalized)
    cleaned = re.sub(r"\s+", "_", cleaned).strip(" .")
    cleaned = cleaned[:max_length].rstrip(" .") or fallback
    if cleaned.split(".", 1)[0].upper() in _WINDOWS_RESERVED:
        cleaned = f"_{cleaned}"
    return cleaned


def post_relative_dir(post: PostMetadata) -> Path:
    handle = sanitize_component(post.author_handle.lstrip("@"), "unknown")
    post_id = re.sub(r"[^0-9A-Za-z_-]", "_", post.post_id)[:80].strip(" ._") or "post"
    if post_id.upper() in _WINDOWS_RESERVED:
        post_id = f"_{post_id}"
    return Path(f"@{handle}") / post_id


def quality_token(quality: QualityOption | None) -> str | None:
    if not quality:
        return None
    if quality.height:
        return f"{quality.height}p"
    value = quality.id.removeprefix("q-")
    return sanitize_component(value, "quality", 24)


def media_filename(attachment: Attachment, quality: QualityOption | None = None) -> str:
    extension = re.sub(r"[^A-Za-z0-9]", "", attachment.extension.lower())[:8]
    if not extension:
        extension = "jpg" if attachment.media_type == MediaType.PHOTO else "mp4"
    token = quality_token(quality) if attachment.media_type != MediaType.PHOTO else None
    suffix = f"-{token}" if token else ""
    return f"{attachment.index:02d}{suffix}.{extension}"


def build_export_layout(destination: Path, analysis: Analysis) -> ExportLayout:
    root = destination.expanduser().resolve()
    output_dir = root / post_relative_dir(analysis.post)
    layout = ExportLayout(
        root=root,
        output_dir=output_dir,
        media_dir=output_dir / "media",
        markdown_path=output_dir / "post.md",
        pdf_path=output_dir / "post.pdf",
    )
    validate_export_paths(layout)
    return layout


def validate_export_paths(layout: ExportLayout) -> None:
    try:
        layout.output_dir.relative_to(layout.root)
    except ValueError as error:
        raise LayoutError(
            "The post output folder escapes the configured download folder."
        ) from error
    root_parts = layout.root.parts
    is_windows_mount = len(root_parts) >= 3 and root_parts[:2] == ("/", "mnt")
    if is_windows_mount:
        longest = max(
            len(str(path))
            for path in (
                layout.media_dir / ("00-1080p.mp4.part"),
                layout.markdown_path.with_suffix(".md.part"),
                layout.pdf_path.with_suffix(".pdf.part"),
            )
        )
        if longest > 240:
            raise LayoutError(
                "The selected Windows download path is too long for reliable exports."
            )
