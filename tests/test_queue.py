import asyncio
import os
import signal
import sys
from contextlib import suppress
from pathlib import Path

import pytest

import x_media_downloader.queue as queue_module
from x_media_downloader.database import Database
from x_media_downloader.models import (
    Analysis,
    Attachment,
    AttachmentSelection,
    Job,
    JobStatus,
    MediaType,
    OutputFormat,
    PostMetadata,
    QualityOption,
)
from x_media_downloader.queue import (
    DownloadQueue,
    QueueError,
    estimated_total,
    filename_base,
    output_lock,
    safe_component,
    ytdlp_progress_arguments,
)


def video_analysis() -> Analysis:
    return Analysis(
        id="analysis-video",
        url="https://x.com/i/web/status/2078098073901834651",
        post=PostMetadata(
            post_id="2078098073901834651",
            author_name="Raytar",
            author_handle="Raytar",
        ),
        attachments=[
            Attachment(
                id="a-1",
                index=1,
                media_type=MediaType.VIDEO,
                extension="mp4",
                qualities=[
                    QualityOption(
                        id="q-1080",
                        label="1080p · Best",
                        selector="http-10368/bv*+ba/b",
                        size_bytes=1_059_845_532,
                    )
                ],
            )
        ],
    )


def test_filename_and_estimated_size() -> None:
    analysis = video_analysis()
    selection = AttachmentSelection(attachment_id="a-1", quality_id="q-1080")
    assert filename_base(analysis, analysis.attachments[0]) == "@Raytar_2078098073901834651_01"
    assert estimated_total(analysis, [selection]) == 1_059_845_532
    assert safe_component('bad:name/with*chars') == "bad_name_with_chars"


def test_ytdlp_progress_is_forced_when_after_move_printing_is_used() -> None:
    arguments = ytdlp_progress_arguments()
    assert "--progress" in arguments
    assert "--progress-template" in arguments
    assert any("XMD_PROGRESS" in value for value in arguments)


def test_output_lock_rejects_a_second_writer(tmp_path: Path) -> None:
    target = tmp_path / "video"
    with (
        output_lock(target),
        pytest.raises(QueueError, match="already writing"),
        output_lock(target),
    ):
        pass


@pytest.mark.asyncio
async def test_queued_job_pause_cancel_and_retry(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.db")
    analysis = video_analysis()
    database.save_analysis(analysis)
    job = Job(
        id="job-1",
        analysis_id=analysis.id,
        url=analysis.url,
        post=analysis.post,
        selections=[AttachmentSelection(attachment_id="a-1", quality_id="q-1080")],
        destination=str(tmp_path),
    )
    database.save_job(job)
    queue = DownloadQueue(database)
    paused = await queue.pause(job.id)
    assert paused.status == JobStatus.PAUSED
    cancelled = await queue.cancel(job.id)
    assert cancelled.status == JobStatus.CANCELLED
    assert "partial data kept" in cancelled.phase
    retried = await queue.retry(job.id)
    assert retried.status == JobStatus.QUEUED
    database.close()


@pytest.mark.asyncio
async def test_markdown_only_text_post_completes_without_attachments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = Database(tmp_path / "state.db")
    analysis = Analysis(
        id="analysis-text",
        url="https://x.com/i/web/status/42",
        post=PostMetadata(
            post_id="42",
            author_name="Writer",
            author_handle="writer",
            text="A useful post",
        ),
        attachments=[],
    )
    database.save_analysis(analysis)
    job = Job(
        id="job-text",
        analysis_id=analysis.id,
        url=analysis.url,
        post=analysis.post,
        selections=[],
        outputs=[OutputFormat.MARKDOWN],
        destination=str(tmp_path),
    )
    database.save_job(job)
    monkeypatch.setattr(queue_module, "document_base", lambda _: "@writer_42")
    monkeypatch.setattr(queue_module, "render_markdown", lambda *_: b"# A useful post\n")
    monkeypatch.setattr(queue_module, "verify_markdown", lambda *_: None)

    queue = DownloadQueue(database)
    await queue._execute(job)

    completed = database.get_job(job.id)
    assert completed is not None
    assert completed.status == JobStatus.COMPLETED
    assert completed.progress == 100
    assert completed.total_steps == 1
    assert (tmp_path / "@writer_42.md").read_text() == "# A useful post\n"
    assert completed.completed_files == [str(tmp_path / "@writer_42.md")]
    database.close()


@pytest.mark.asyncio
async def test_multiple_photos_refresh_gallery_links_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = Database(tmp_path / "state.db")
    analysis = Analysis(
        id="analysis-photos",
        url="https://x.com/i/web/status/84",
        post=PostMetadata(
            post_id="84", author_name="Artist", author_handle="artist"
        ),
        attachments=[
            Attachment(
                id=f"a-{index}",
                index=index,
                media_type=MediaType.PHOTO,
                extension="jpg",
            )
            for index in (1, 2)
        ],
    )
    database.save_analysis(analysis)
    job = Job(
        id="job-photos",
        analysis_id=analysis.id,
        url=analysis.url,
        post=analysis.post,
        selections=[
            AttachmentSelection(attachment_id="a-1"),
            AttachmentSelection(attachment_id="a-2"),
        ],
        destination=str(tmp_path),
    )
    database.save_job(job)
    resolved = [object(), object()]
    resolve_calls = 0

    async def fake_resolve(_url: str):
        nonlocal resolve_calls
        resolve_calls += 1
        return {}, resolved

    async def fake_download(
        _job,
        _analysis,
        attachment,
        destination,
        _completed_base,
        *,
        target=None,
        resolved_items=None,
    ):
        assert resolved_items is resolved
        path = target or destination / f"photo-{attachment.index}.jpg"
        path.write_bytes(b"image")
        return path, 5

    queue = DownloadQueue(database)
    monkeypatch.setattr(queue_module, "resolve_gallery_media", fake_resolve)
    monkeypatch.setattr(queue, "_download_photo", fake_download)
    await queue._execute(job)

    completed = database.get_job(job.id)
    assert completed is not None
    assert completed.status == JobStatus.COMPLETED
    assert resolve_calls == 1
    database.close()


@pytest.mark.asyncio
async def test_v2_combined_export_uses_one_organized_media_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = Database(tmp_path / "state.db")
    analysis = Analysis(
        id="analysis-organized",
        url="https://x.com/i/web/status/84",
        post=PostMetadata(
            post_id="84", author_name="Artist", author_handle="artist"
        ),
        attachments=[
            Attachment(
                id="a-1", index=1, media_type=MediaType.PHOTO, extension="jpg"
            )
        ],
    )
    database.save_analysis(analysis)
    output_dir = tmp_path / "@artist" / "84"
    job = Job(
        id="job-organized",
        analysis_id=analysis.id,
        url=analysis.url,
        post=analysis.post,
        selections=[AttachmentSelection(attachment_id="a-1")],
        outputs=[OutputFormat.MEDIA, OutputFormat.MARKDOWN, OutputFormat.PDF],
        destination=str(tmp_path),
        layout_version=2,
        output_dir=str(output_dir),
    )
    database.save_job(job)
    resolved = [object()]

    async def fake_resolve(_url: str):
        return {}, resolved

    async def fake_download(
        _job,
        _analysis,
        _attachment,
        _destination,
        _completed_base,
        *,
        target=None,
        resolved_items=None,
    ):
        assert resolved_items is resolved
        assert target == output_dir / "media" / "01.jpg"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"image")
        return target, 5

    def fake_markdown(_analysis, media_paths):
        assert media_paths == {"a-1": output_dir / "media" / "01.jpg"}
        return b"![image](media/01.jpg)\n"

    def fake_pdf(_analysis, media_paths):
        assert media_paths == {"a-1": output_dir / "media" / "01.jpg"}
        return b"pdf"

    queue = DownloadQueue(database)
    monkeypatch.setattr(queue_module, "resolve_gallery_media", fake_resolve)
    monkeypatch.setattr(queue, "_download_photo", fake_download)
    monkeypatch.setattr(queue_module, "render_markdown", fake_markdown)
    monkeypatch.setattr(queue_module, "render_pdf", fake_pdf)
    monkeypatch.setattr(queue_module, "verify_markdown", lambda *_: None)
    monkeypatch.setattr(queue_module, "verify_pdf", lambda *_: None)

    await queue._execute(job)

    completed = database.get_job(job.id)
    assert completed is not None
    assert completed.status == JobStatus.COMPLETED
    assert completed.completed_files == [
        str(output_dir / "media" / "01.jpg"),
        str(output_dir / "post.md"),
        str(output_dir / "post.pdf"),
    ]
    assert list(output_dir.rglob("01.jpg")) == [output_dir / "media" / "01.jpg"]
    database.close()


@pytest.mark.asyncio
async def test_recovery_terminates_owned_stale_worker_before_requeue(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.db")
    analysis = video_analysis()
    database.save_analysis(analysis)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import time; time.sleep(30)",
        "yt_dlp",
        analysis.post.post_id,
        start_new_session=True,
    )
    job = Job(
        id="stale-job",
        analysis_id=analysis.id,
        url=analysis.url,
        post=analysis.post,
        selections=[AttachmentSelection(attachment_id="a-1", quality_id="q-1080")],
        destination=str(tmp_path),
        status=JobStatus.RUNNING,
        worker_pid=process.pid,
        worker_pgid=os.getpgid(process.pid),
    )
    database.save_job(job)
    queue = DownloadQueue(database)
    try:
        await queue._recover_jobs()
        await asyncio.wait_for(process.wait(), timeout=3)
        recovered = database.get_job(job.id)
        assert recovered is not None
        assert recovered.status == JobStatus.QUEUED
        assert recovered.worker_pid is None
        assert recovered.worker_pgid is None
    finally:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await process.wait()
        database.close()
