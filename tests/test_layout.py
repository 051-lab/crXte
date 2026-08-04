from pathlib import Path

import pytest

from x_media_downloader.layout import (
    LayoutError,
    build_export_layout,
    media_filename,
    post_relative_dir,
    sanitize_component,
)
from x_media_downloader.models import (
    Analysis,
    Attachment,
    MediaType,
    PostMetadata,
    QualityOption,
)


def analysis(handle: str = "creator", post_id: str = "42") -> Analysis:
    return Analysis(
        id="analysis",
        url=f"https://x.com/i/web/status/{post_id}",
        post=PostMetadata(
            post_id=post_id, author_name="Creator", author_handle=handle
        ),
        attachments=[],
    )


def test_post_layout_groups_by_windows_safe_author_and_post(tmp_path: Path) -> None:
    item = analysis("CON", "42")
    layout = build_export_layout(tmp_path, item)

    assert post_relative_dir(item.post) == Path("@_CON") / "42"
    assert layout.output_dir == tmp_path / "@_CON" / "42"
    assert layout.media_dir == layout.output_dir / "media"
    assert layout.markdown_path == layout.output_dir / "post.md"
    assert layout.pdf_path == layout.output_dir / "post.pdf"


def test_component_normalizes_unicode_and_invalid_windows_characters() -> None:
    assert sanitize_component("  Ａ / name. ", "x") == "A___name"
    assert sanitize_component("__creator", "x") == "__creator"
    assert sanitize_component("NUL", "x") == "_NUL"
    assert sanitize_component("con.txt", "x") == "_con.txt"


def test_media_filename_distinguishes_video_quality() -> None:
    attachment = Attachment(
        id="a-2", index=2, media_type=MediaType.VIDEO, extension="mp4"
    )
    quality = QualityOption(
        id="q-1080", label="1080p", selector="best", height=1080
    )
    assert media_filename(attachment, quality) == "02-1080p.mp4"
    assert media_filename(attachment) == "02.mp4"


def test_windows_mount_rejects_excessive_final_path() -> None:
    destination = Path("/mnt/c") / ("x" * 230)
    with pytest.raises(LayoutError, match="too long"):
        build_export_layout(destination, analysis())
