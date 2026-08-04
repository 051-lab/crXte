from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import x_media_downloader.verification as verification
from x_media_downloader.documents import write_pdf
from x_media_downloader.models import Attachment, MediaType, PostMetadata, QualityOption
from x_media_downloader.verification import (
    VerificationError,
    verify_markdown,
    verify_media,
    verify_pdf,
)


def make_video(path: Path) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=320x180:d=1",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=44100:cl=stereo",
            "-shortest",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            str(path),
        ],
        check=True,
        timeout=30,
    )


@pytest.mark.asyncio
async def test_verify_media_checks_streams_dimensions_duration_and_size(tmp_path: Path) -> None:
    path = tmp_path / "tiny.mp4"
    make_video(path)
    attachment = Attachment(
        id="a-1",
        index=1,
        media_type=MediaType.VIDEO,
        extension="mp4",
        width=320,
        height=180,
        duration=1.0,
    )
    quality = QualityOption(
        id="q-180",
        label="180p",
        selector="test",
        width=320,
        height=180,
        size_bytes=path.stat().st_size,
    )
    assert await verify_media(path, attachment, quality) == path.stat().st_size


@pytest.mark.asyncio
async def test_verify_media_rejects_exact_size_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "tiny.mp4"
    make_video(path)
    attachment = Attachment(
        id="a-1",
        index=1,
        media_type=MediaType.VIDEO,
        extension="mp4",
        height=180,
        duration=1.0,
    )
    quality = QualityOption(
        id="q-180",
        label="180p",
        selector="test",
        height=180,
        size_bytes=path.stat().st_size + 1_000_000,
    )
    with pytest.raises(VerificationError, match="expected"):
        await verify_media(path, attachment, quality)


@pytest.mark.asyncio
async def test_verify_media_reaps_timed_out_ffprobe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "media.jpg"
    path.write_bytes(b"image")

    class TimedOutProcess:
        returncode = None

        def __init__(self) -> None:
            self.killed = False
            self.waited = False

        async def communicate(self):
            return b"", b""

        def kill(self) -> None:
            self.killed = True

        async def wait(self) -> None:
            self.waited = True

    process = TimedOutProcess()

    async def create_process(*_args, **_kwargs):
        return process

    async def time_out(awaitable, *, timeout):
        awaitable.close()
        assert timeout == 30
        raise TimeoutError

    monkeypatch.setattr(verification.asyncio, "create_subprocess_exec", create_process)
    monkeypatch.setattr(verification.asyncio, "wait_for", time_out)
    attachment = Attachment(
        id="a-1", index=1, media_type=MediaType.PHOTO, extension="jpg"
    )

    with pytest.raises(VerificationError, match="timed out"):
        await verify_media(path, attachment)
    assert process.killed
    assert process.waited


def test_verify_markdown_preserves_exact_utf8_and_checks_assets(tmp_path: Path) -> None:
    path = tmp_path / "post.md"
    content = "# Café — Привет\n\n![Photo](<media/photo%20one.png>)\n".encode()
    path.write_bytes(content)
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    asset = media_dir / "photo one.png"
    asset.write_bytes(b"image")

    assert verify_markdown(path, content, [asset]) == len(content)

    with pytest.raises(VerificationError, match="differs"):
        verify_markdown(path, content + b"changed", [asset])
    asset.unlink()
    with pytest.raises(VerificationError, match="asset is missing"):
        verify_markdown(path, content, [asset])


@pytest.mark.parametrize(
    "target",
    ["../secret.png", "media/%2e%2e/secret.png", "/etc/passwd", r"..\secret.png"],
)
def test_verify_markdown_rejects_unsafe_local_targets(tmp_path: Path, target: str) -> None:
    path = tmp_path / "unsafe.md"
    path.write_text(f"[unsafe](<{target}>)\n", encoding="utf-8")

    with pytest.raises(VerificationError, match="unsafe link"):
        verify_markdown(path)


def test_verify_pdf_preserves_exact_deterministic_bytes(tmp_path: Path) -> None:
    path = tmp_path / "post.pdf"
    post = PostMetadata(
        post_id="123",
        author_name="Renée Example",
        author_handle="example",
        text="Café — Привет",
        posted_at="2026-07-25T12:00:00+00:00",
    )
    write_pdf(path, post, [])
    expected = path.read_bytes()

    assert verify_pdf(path, expected) == len(expected)
    with pytest.raises(VerificationError, match="differs"):
        verify_pdf(path, expected + b"changed")

