from __future__ import annotations

import asyncio
import fcntl
import hashlib
import os
import re
import shutil
import signal
import sys
import time
from contextlib import contextmanager, suppress
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .config import DATA_DIR
from .database import Database
from .documents import (
    DocumentError,
    document_base,
    markdown_assets_name,
    render_markdown,
    render_pdf,
)
from .extractors import (
    TWIMG_HOSTS,
    ResolvedMedia,
    analyze_url,
    find_resolved_item,
    resolve_gallery_media,
)
from .layout import LayoutError, build_export_layout, media_filename
from .models import (
    Analysis,
    Attachment,
    AttachmentRole,
    AttachmentSelection,
    Job,
    JobStatus,
    MediaType,
    OutputFormat,
    QualityOption,
    utc_now,
)
from .verification import VerificationError, verify_markdown, verify_media, verify_pdf


class DownloadCancelled(RuntimeError):
    pass


class QueueError(RuntimeError):
    pass


YTDLP_PROGRESS_TEMPLATE = (
    "download:XMD_PROGRESS:%(progress.downloaded_bytes)s|"
    "%(progress.total_bytes,progress.total_bytes_estimate)s|"
    "%(progress.speed)s|%(progress.eta)s"
)


def ytdlp_progress_arguments() -> list[str]:
    # --print implies quiet mode in yt-dlp, so --progress must be explicit.
    return ["--newline", "--progress", "--progress-template", YTDLP_PROGRESS_TEMPLATE]


@contextmanager
def output_lock(target: Path):
    lock_dir = DATA_DIR / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(str(target).encode()).hexdigest()
    lock_file = (lock_dir / f"{digest}.lock").open("a+")
    try:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise QueueError("Another download worker is already writing this output.") from error
        yield
    finally:
        with suppress(OSError):
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()


def safe_component(value: str, fallback: str = "media") -> str:
    cleaned = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "_", value).strip(" .")
    cleaned = re.sub(r"\s+", "_", cleaned)
    return cleaned[:80] or fallback


def filename_base(analysis: Analysis, attachment: Attachment) -> str:
    handle = safe_component(analysis.post.author_handle, "x")
    return f"@{handle}_{analysis.post.post_id}_{attachment.index:02d}"


def selected_quality(
    attachment: Attachment, selection: AttachmentSelection
) -> QualityOption | None:
    if not attachment.qualities:
        return None
    if selection.quality_id:
        found = next(
            (item for item in attachment.qualities if item.id == selection.quality_id), None
        )
        if found:
            return found
    return attachment.qualities[0]


def estimated_total(analysis: Analysis, selections: list[AttachmentSelection]) -> int | None:
    lookup = {item.id: item for item in analysis.attachments}
    total = 0
    known = False
    for selection in selections:
        attachment = lookup.get(selection.attachment_id)
        if not attachment:
            continue
        quality = selected_quality(attachment, selection)
        size = quality.size_bytes if quality else attachment.size_bytes
        if size:
            total += size
            known = True
    return total if known else None


class DownloadQueue:
    def __init__(self, database: Database):
        self.database = database
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._stopping = False
        self._active_job_id: str | None = None
        self._active_process: asyncio.subprocess.Process | None = None
        self._pause_gate = asyncio.Event()
        self._pause_gate.set()
        self._cancel_requested = False
        self._last_progress_save = 0.0
        self._last_saved_progress = -1.0

    async def start(self) -> None:
        await self._recover_jobs()
        self._task = asyncio.create_task(self._run_loop())
        self._wake.set()

    async def stop(self) -> None:
        self._stopping = True
        self._cancel_requested = True
        self._pause_gate.set()
        await self._stop_process(signal.SIGTERM)
        self._wake.set()
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        if self._active_job_id:
            job = self.database.get_job(self._active_job_id)
            if job:
                if job.status != JobStatus.PAUSED:
                    job.status = JobStatus.QUEUED
                    job.phase = "Interrupted · queued to resume"
                job.worker_pid = None
                job.worker_pgid = None
                job.heartbeat_at = None
                self.database.save_job(job)

    @property
    def is_running(self) -> bool:
        return bool(self._task and not self._task.done())

    async def _recover_jobs(self) -> None:
        for job in self.database.list_jobs(500):
            if job.status not in {JobStatus.RUNNING, JobStatus.PAUSED}:
                continue
            if job.worker_pid and job.worker_pgid and self._worker_matches(job):
                await self._terminate_worker(job.worker_pid, job.worker_pgid)
            job.worker_pid = None
            job.worker_pgid = None
            job.heartbeat_at = None
            if job.status == JobStatus.RUNNING:
                job.status = JobStatus.QUEUED
                job.phase = "Recovered safely after restart"
                job.speed = None
                job.eta = None
            self.database.save_job(job)

    @staticmethod
    def _worker_matches(job: Job) -> bool:
        if not job.worker_pid:
            return False
        try:
            command = Path(f"/proc/{job.worker_pid}/cmdline").read_bytes().replace(b"\0", b" ")
        except OSError:
            return False
        return b"yt_dlp" in command and job.post.post_id.encode() in command

    @staticmethod
    async def _terminate_worker(pid: int, pgid: int) -> None:
        with suppress(ProcessLookupError):
            os.killpg(pgid, signal.SIGCONT)
            os.killpg(pgid, signal.SIGTERM)
        for _ in range(20):
            if not Path(f"/proc/{pid}").exists():
                return
            await asyncio.sleep(0.1)
        with suppress(ProcessLookupError):
            os.killpg(pgid, signal.SIGKILL)

    def notify(self) -> None:
        self._wake.set()

    async def pause(self, job_id: str) -> Job:
        job = self._require_job(job_id)
        if job.status == JobStatus.QUEUED:
            job.status = JobStatus.PAUSED
            job.phase = "Paused in queue"
        elif job.status == JobStatus.RUNNING and self._active_job_id == job_id:
            self._pause_gate.clear()
            if self._active_process and self._active_process.returncode is None:
                os.killpg(self._active_process.pid, signal.SIGSTOP)
            job.status = JobStatus.PAUSED
            job.phase = "Paused"
            job.speed = None
            job.eta = None
        self.database.save_job(job)
        return job

    async def resume(self, job_id: str) -> Job:
        job = self._require_job(job_id)
        if job.status != JobStatus.PAUSED:
            return job
        if self._active_job_id == job_id:
            if self._active_process and self._active_process.returncode is None:
                os.killpg(self._active_process.pid, signal.SIGCONT)
            self._pause_gate.set()
            job.status = JobStatus.RUNNING
            job.phase = "Resuming"
        else:
            job.status = JobStatus.QUEUED
            job.phase = "Queued to resume"
            self.notify()
        self.database.save_job(job)
        return job

    async def cancel(self, job_id: str) -> Job:
        job = self._require_job(job_id)
        if job.status in {JobStatus.COMPLETED, JobStatus.CANCELLED}:
            return job
        if self._active_job_id == job_id:
            self._cancel_requested = True
            self._pause_gate.set()
            if self._active_process and self._active_process.returncode is None:
                with suppress(ProcessLookupError):
                    os.killpg(self._active_process.pid, signal.SIGCONT)
                await self._stop_process(signal.SIGINT)
            job.phase = "Cancelling"
        else:
            job.status = JobStatus.CANCELLED
            job.phase = "Cancelled · partial data kept"
        self.database.save_job(job)
        return job

    async def retry(self, job_id: str) -> Job:
        job = self._require_job(job_id)
        if job.status not in {JobStatus.FAILED, JobStatus.CANCELLED}:
            return job
        job.status = JobStatus.QUEUED
        job.phase = "Queued to retry"
        job.error = None
        job.progress = 0
        job.downloaded_bytes = 0
        job.completed_steps = 0
        job.current_attachment = None
        job.speed = None
        job.eta = None
        self.database.save_job(job)
        self.notify()
        return job

    def _require_job(self, job_id: str) -> Job:
        job = self.database.get_job(job_id)
        if not job:
            raise QueueError("Download job not found.")
        return job

    async def _run_loop(self) -> None:
        while not self._stopping:
            jobs = [job for job in self.database.list_jobs(500) if job.status == JobStatus.QUEUED]
            if not jobs:
                self._wake.clear()
                await self._wake.wait()
                continue
            job = sorted(jobs, key=lambda item: item.created_at)[0]
            try:
                await self._execute(job)
            except asyncio.CancelledError:
                raise
            except Exception as error:  # keep the persistent worker alive on unexpected faults
                failed = self.database.get_job(job.id) or job
                failed.status = JobStatus.FAILED
                failed.phase = "Unexpected queue failure"
                failed.error = str(error)[-400:]
                failed.worker_pid = None
                failed.worker_pgid = None
                failed.heartbeat_at = None
                self.database.save_job(failed)

    async def _execute(self, job: Job) -> None:
        if job.layout_version >= 2:
            await self._execute_v2(job)
        else:
            await self._execute_legacy(job)

    async def _execute_v2(self, job: Job) -> None:
        analysis = self.database.get_analysis(job.analysis_id)
        if not analysis:
            job.status = JobStatus.FAILED
            job.error = "The saved analysis is missing. Analyze the link again."
            self.database.save_job(job)
            return
        outputs = set(job.outputs)
        wants_documents = bool(outputs & {OutputFormat.MARKDOWN, OutputFormat.PDF})
        if (
            wants_documents
            and analysis.article
            and analysis.article.html_renderer_version < 1
        ):
            try:
                refreshed = await analyze_url(analysis.url)
                if (
                    not refreshed.article
                    or refreshed.article.html_renderer_version < 1
                ):
                    raise QueueError("X did not return corrected article content.")
            except Exception:
                job.status = JobStatus.FAILED
                job.phase = "Export failed"
                job.error = (
                    "This saved article uses an outdated renderer. Analyze the link again."
                )
                self.database.save_job(job)
                return
            refreshed.id = analysis.id
            refreshed.created_at = analysis.created_at
            refreshed.output_relative_dir = analysis.output_relative_dir
            self.database.save_analysis(refreshed)
            analysis = refreshed
            job.post = analysis.post
            job.content_kind = analysis.content_kind
            job.article = analysis.article

        try:
            layout = build_export_layout(Path(job.destination), analysis)
        except LayoutError as error:
            job.status = JobStatus.FAILED
            job.phase = "Export failed"
            job.error = str(error)
            self.database.save_job(job)
            return
        if job.output_dir and Path(job.output_dir) != layout.output_dir:
            job.status = JobStatus.FAILED
            job.phase = "Export failed"
            job.error = "The saved post output folder no longer matches this analysis."
            self.database.save_job(job)
            return
        job.output_dir = str(layout.output_dir)
        self._active_job_id = job.id
        self._cancel_requested = False
        self._last_progress_save = 0.0
        self._last_saved_progress = -1.0
        self._pause_gate.set()
        job.status = JobStatus.RUNNING
        job.phase = "Preparing post folder"
        job.error = None
        job.completed_steps = 0
        self.database.save_job(job)
        try:
            layout.output_dir.mkdir(parents=True, exist_ok=True)
            if not os.access(layout.output_dir, os.W_OK):
                raise QueueError("The post output folder is not writable.")
            attachment_lookup = {item.id: item for item in analysis.attachments}
            selected: list[tuple[AttachmentSelection, Attachment]] = []
            for selection in job.selections:
                attachment = attachment_lookup.get(selection.attachment_id)
                if not attachment:
                    raise QueueError(
                        f"Attachment {selection.attachment_id} is no longer available."
                    )
                selected.append((selection, attachment))

            needs_media = OutputFormat.MEDIA in outputs or (
                wants_documents and job.include_document_media
            )
            downloads = selected if needs_media else []
            job.total_bytes = estimated_total(
                analysis, [selection for selection, _ in downloads]
            )
            job.total_steps = (
                len(downloads)
                + int(OutputFormat.MARKDOWN in outputs)
                + int(OutputFormat.PDF in outputs)
            )
            required = (job.total_bytes or 64 * 1024**2) + max(
                128 * 1024**2, int((job.total_bytes or 0) * 0.1)
            )
            if shutil.disk_usage(layout.output_dir).free < required:
                raise QueueError("There is not enough free space for this export.")

            direct_downloads = [
                attachment
                for _, attachment in downloads
                if attachment.media_type == MediaType.PHOTO
                or attachment.role == AttachmentRole.ARTICLE_VIDEO
            ]
            resolved_items: list[ResolvedMedia] | None = None
            if direct_downloads:
                await self._checkpoint()
                job.phase = "Refreshing media links"
                self.database.save_job(job)
                _, resolved_items = await resolve_gallery_media(analysis.url)

            completed_bytes = 0
            media_paths: dict[str, Path] = {}
            for selection, attachment in downloads:
                await self._checkpoint()
                layout.media_dir.mkdir(parents=True, exist_ok=True)
                job.current_attachment = attachment.index
                quality = selected_quality(attachment, selection)
                expected = quality.size_bytes if quality else attachment.size_bytes
                target = layout.media_dir / media_filename(attachment, quality)
                job.phase = f"Downloading media {attachment.index} of {len(downloads)}"
                self.database.save_job(job)
                if (
                    attachment.media_type == MediaType.PHOTO
                    or attachment.role == AttachmentRole.ARTICLE_VIDEO
                ):
                    path, actual = await self._download_photo(
                        job,
                        analysis,
                        attachment,
                        layout.media_dir,
                        completed_bytes,
                        target=target,
                        resolved_items=resolved_items,
                    )
                else:
                    path, actual = await self._download_video(
                        job,
                        analysis,
                        attachment,
                        quality,
                        layout.media_dir,
                        completed_bytes,
                        target_base=target.with_suffix(""),
                    )
                media_paths[attachment.id] = path
                if str(path) not in job.completed_files:
                    job.completed_files.append(str(path))
                completed_bytes += actual or expected or 0
                self._complete_step(job, completed_bytes)

            job.current_attachment = None
            document_media = media_paths if job.include_document_media else {}
            if OutputFormat.MARKDOWN in outputs:
                await self._checkpoint()
                job.phase = "Rendering Markdown"
                self.database.save_job(job)
                markdown = await asyncio.to_thread(render_markdown, analysis, document_media)
                await self._checkpoint()
                await asyncio.to_thread(
                    self._write_document,
                    layout.markdown_path,
                    markdown,
                    verify_markdown,
                    list(document_media.values()),
                )
                if str(layout.markdown_path) not in job.completed_files:
                    job.completed_files.append(str(layout.markdown_path))
                self._complete_step(job, completed_bytes)

            if OutputFormat.PDF in outputs:
                await self._checkpoint()
                job.phase = "Rendering PDF"
                self.database.save_job(job)
                pdf = await asyncio.to_thread(render_pdf, analysis, document_media)
                await self._checkpoint()
                await asyncio.to_thread(
                    self._write_document, layout.pdf_path, pdf, verify_pdf
                )
                if str(layout.pdf_path) not in job.completed_files:
                    job.completed_files.append(str(layout.pdf_path))
                self._complete_step(job, completed_bytes)

            job.status = JobStatus.COMPLETED
            job.phase = f"Complete · {len(job.completed_files)} output(s)"
            job.progress = 100
            job.speed = None
            job.eta = None
        except DownloadCancelled:
            job.status = JobStatus.CANCELLED
            job.phase = "Cancelled · partial data kept"
            job.speed = None
            job.eta = None
        except (
            DocumentError,
            QueueError,
            VerificationError,
            httpx.HTTPError,
            OSError,
        ) as error:
            job.status = JobStatus.FAILED
            job.phase = "Export failed"
            job.error = str(error)
            job.speed = None
            job.eta = None
        finally:
            self._active_process = None
            self._active_job_id = None
            job.worker_pid = None
            job.worker_pgid = None
            job.heartbeat_at = None
            self.database.save_job(job)

    async def _execute_legacy(self, job: Job) -> None:
        analysis = self.database.get_analysis(job.analysis_id)
        if not analysis:
            job.status = JobStatus.FAILED
            job.error = "The saved analysis is missing. Analyze the link again."
            self.database.save_job(job)
            return
        self._active_job_id = job.id
        self._cancel_requested = False
        self._last_progress_save = 0.0
        self._last_saved_progress = -1.0
        self._pause_gate.set()
        job.status = JobStatus.RUNNING
        job.phase = "Preparing destination"
        job.error = None
        job.completed_steps = 0
        self.database.save_job(job)
        temporary_assets: Path | None = None
        try:
            destination = Path(job.destination).expanduser().resolve()
            destination.mkdir(parents=True, exist_ok=True)
            if not os.access(destination, os.W_OK):
                raise QueueError("The export folder is not writable.")

            attachment_lookup = {item.id: item for item in analysis.attachments}
            selected = []
            for selection in job.selections:
                attachment = attachment_lookup.get(selection.attachment_id)
                if not attachment:
                    raise QueueError(
                        f"Attachment {selection.attachment_id} is no longer available."
                    )
                selected.append((selection, attachment))

            outputs = set(job.outputs)
            wants_documents = bool(outputs & {OutputFormat.MARKDOWN, OutputFormat.PDF})
            document_photos = [
                (selection, attachment)
                for selection, attachment in selected
                if job.include_document_media and attachment.media_type == MediaType.PHOTO
            ]
            network_selections = (
                job.selections
                if OutputFormat.MEDIA in outputs
                else [selection for selection, _ in document_photos]
            )
            job.total_bytes = estimated_total(analysis, network_selections)
            asset_steps = 0
            if (
                wants_documents
                and document_photos
                and (
                    OutputFormat.MARKDOWN in outputs
                    or OutputFormat.MEDIA not in outputs
                )
            ):
                asset_steps = len(document_photos)
            job.total_steps = (
                (len(selected) if OutputFormat.MEDIA in outputs else 0)
                + asset_steps
                + int(OutputFormat.MARKDOWN in outputs)
                + int(OutputFormat.PDF in outputs)
            )
            required = (job.total_bytes or 64 * 1024**2) + max(
                128 * 1024**2, int((job.total_bytes or 0) * 0.1)
            )
            if shutil.disk_usage(destination).free < required:
                raise QueueError("There is not enough free space in the export folder.")
            self.database.save_job(job)

            direct_downloads = [
                attachment
                for _, attachment in selected
                if attachment.media_type == MediaType.PHOTO
                or attachment.role == AttachmentRole.ARTICLE_VIDEO
            ]
            resolved_items: list[ResolvedMedia] | None = None
            if direct_downloads and (
                OutputFormat.MEDIA in outputs or (wants_documents and document_photos)
            ):
                await self._checkpoint()
                job.phase = "Refreshing media links"
                self.database.save_job(job)
                _, resolved_items = await resolve_gallery_media(analysis.url)

            completed_bytes = 0
            media_paths: dict[str, Path] = {}
            if OutputFormat.MEDIA in outputs:
                for selection, attachment in selected:
                    await self._checkpoint()
                    job.current_attachment = attachment.index
                    quality = selected_quality(attachment, selection)
                    expected = quality.size_bytes if quality else attachment.size_bytes
                    job.phase = (
                        f"Downloading attachment {attachment.index} of {len(selected)}"
                    )
                    self.database.save_job(job)
                    if (
                        attachment.media_type == MediaType.PHOTO
                        or attachment.role == AttachmentRole.ARTICLE_VIDEO
                    ):
                        path, actual = await self._download_photo(
                            job,
                            analysis,
                            attachment,
                            destination,
                            completed_bytes,
                            resolved_items=resolved_items,
                        )
                    else:
                        path, actual = await self._download_video(
                            job, analysis, attachment, quality, destination, completed_bytes
                        )
                    media_paths[attachment.id] = path
                    if str(path) not in job.completed_files:
                        job.completed_files.append(str(path))
                    completed_bytes += actual or expected or 0
                    self._complete_step(job, completed_bytes)

            document_media: dict[str, Path] = (
                dict(media_paths) if job.include_document_media else {}
            )
            if wants_documents and document_photos:
                base = document_base(analysis)
                if OutputFormat.MARKDOWN in outputs:
                    assets_dir = destination / markdown_assets_name(analysis)
                elif OutputFormat.MEDIA not in outputs:
                    temporary_assets = destination / f".{base}_assets.part"
                    assets_dir = temporary_assets
                else:
                    assets_dir = destination
                for _selection, attachment in document_photos:
                    source = media_paths.get(attachment.id)
                    if OutputFormat.MARKDOWN in outputs:
                        extension = safe_component(attachment.extension.lower(), "jpg")
                        target = assets_dir / f"{attachment.index:02d}.{extension}"
                        if source:
                            await self._materialize_asset(job, attachment, source, target)
                            actual = 0
                        else:
                            await self._checkpoint()
                            job.current_attachment = attachment.index
                            job.phase = f"Preparing document image {attachment.index}"
                            self.database.save_job(job)
                            target, actual = await self._download_photo(
                                job,
                                analysis,
                                attachment,
                                destination,
                                completed_bytes,
                                target=target,
                                resolved_items=resolved_items,
                            )
                        document_media[attachment.id] = target
                        completed_bytes += actual
                        self._complete_step(job, completed_bytes)
                    elif source:
                        document_media[attachment.id] = source
                    else:
                        await self._checkpoint()
                        extension = safe_component(attachment.extension.lower(), "jpg")
                        target = assets_dir / f"{attachment.index:02d}.{extension}"
                        job.current_attachment = attachment.index
                        job.phase = f"Preparing document image {attachment.index}"
                        self.database.save_job(job)
                        target, actual = await self._download_photo(
                            job,
                            analysis,
                            attachment,
                            destination,
                            completed_bytes,
                            target=target,
                            resolved_items=resolved_items,
                        )
                        document_media[attachment.id] = target
                        completed_bytes += actual
                        self._complete_step(job, completed_bytes)

            job.current_attachment = None
            base = document_base(analysis)
            if OutputFormat.MARKDOWN in outputs:
                await self._checkpoint()
                job.phase = "Rendering Markdown"
                self.database.save_job(job)
                markdown = await asyncio.to_thread(render_markdown, analysis, document_media)
                await self._checkpoint()
                target = destination / f"{base}.md"
                await asyncio.to_thread(
                    self._write_document,
                    target,
                    markdown,
                    verify_markdown,
                    list(document_media.values()),
                )
                if str(target) not in job.completed_files:
                    job.completed_files.append(str(target))
                self._complete_step(job, completed_bytes)

            if OutputFormat.PDF in outputs:
                await self._checkpoint()
                job.phase = "Rendering PDF"
                self.database.save_job(job)
                pdf = await asyncio.to_thread(render_pdf, analysis, document_media)
                await self._checkpoint()
                target = destination / f"{base}.pdf"
                await asyncio.to_thread(
                    self._write_document, target, pdf, verify_pdf
                )
                if str(target) not in job.completed_files:
                    job.completed_files.append(str(target))
                self._complete_step(job, completed_bytes)

            if temporary_assets and temporary_assets.is_dir():
                shutil.rmtree(temporary_assets)
            job.status = JobStatus.COMPLETED
            job.phase = f"Complete · {len(job.completed_files)} output(s)"
            job.progress = 100
            job.speed = None
            job.eta = None
        except DownloadCancelled:
            job.status = JobStatus.CANCELLED
            job.phase = "Cancelled · partial data kept"
            job.speed = None
            job.eta = None
        except (
            DocumentError,
            QueueError,
            VerificationError,
            httpx.HTTPError,
            OSError,
        ) as error:
            job.status = JobStatus.FAILED
            job.phase = "Export failed"
            job.error = str(error)
            job.speed = None
            job.eta = None
        finally:
            self._active_process = None
            self._active_job_id = None
            job.worker_pid = None
            job.worker_pgid = None
            job.heartbeat_at = None
            self.database.save_job(job)

    async def _checkpoint(self) -> None:
        if self._cancel_requested:
            raise DownloadCancelled
        await self._pause_gate.wait()
        if self._cancel_requested:
            raise DownloadCancelled

    def _complete_step(self, job: Job, completed_bytes: int) -> None:
        job.completed_steps += 1
        job.downloaded_bytes = completed_bytes
        if job.total_steps:
            job.progress = min(99.9, job.completed_steps / job.total_steps * 100)
        self.database.save_job(job)

    async def _materialize_asset(
        self, job: Job, attachment: Attachment, source: Path, target: Path
    ) -> None:
        await self._checkpoint()
        target.parent.mkdir(parents=True, exist_ok=True)
        with output_lock(target):
            if target.exists():
                await verify_media(target, attachment)
                return
            job.phase = f"Preparing Markdown image {attachment.index}"
            self.database.save_job(job)
            try:
                os.link(source, target)
                return
            except OSError:
                await asyncio.to_thread(shutil.copy2, source, target)
            if target.stat().st_size != source.stat().st_size:
                with suppress(OSError):
                    target.unlink()
                raise VerificationError("The copied Markdown image is incomplete.")

    @staticmethod
    def _write_document(target: Path, content: bytes, verifier, *args) -> None:
        with output_lock(target):
            if target.exists():
                verifier(target, content, *args)
                return
            part = Path(f"{target}.part")
            part.write_bytes(content)
            verifier(part, content, *args)
            os.replace(part, target)

    async def _download_photo(
        self,
        job: Job,
        analysis: Analysis,
        attachment: Attachment,
        destination: Path,
        completed_base: int,
        *,
        target: Path | None = None,
        resolved_items: list[ResolvedMedia] | None = None,
    ) -> tuple[Path, int]:
        extension = safe_component(attachment.extension.lower(), "jpg")
        target = target or destination / f"{filename_base(analysis, attachment)}.{extension}"
        target.parent.mkdir(parents=True, exist_ok=True)
        with output_lock(target):
            if target.exists():
                job.phase = f"Verifying existing attachment {attachment.index}"
                self.database.save_job(job)
                return target, await verify_media(target, attachment)
            if resolved_items is None:
                _, resolved_items = await resolve_gallery_media(analysis.url)
            resolved = find_resolved_item(resolved_items, attachment.index)
            if not resolved:
                raise QueueError("X no longer exposes this attachment.")
            if (urlsplit(resolved.url).hostname or "").lower() not in TWIMG_HOSTS:
                raise QueueError("X returned an unexpected media host.")
            part = Path(f"{target}.part")
            downloaded = part.stat().st_size if part.exists() else 0
            headers = {"User-Agent": "Mozilla/5.0"}
            if downloaded:
                headers["Range"] = f"bytes={downloaded}-"
            timeout = httpx.Timeout(connect=20, read=60, write=30, pool=20)
            async with (
                httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client,
                client.stream("GET", resolved.url, headers=headers) as response,
            ):
                if response.status_code >= 400:
                    raise QueueError(f"The X media server returned HTTP {response.status_code}.")
                append = downloaded > 0 and response.status_code == 206
                if not append:
                    downloaded = 0
                content_length = int(response.headers.get("content-length") or 0)
                total = downloaded + content_length if content_length else attachment.size_bytes
                with part.open("ab" if append else "wb") as output:
                    async for chunk in response.aiter_bytes(128 * 1024):
                        if self._cancel_requested:
                            raise DownloadCancelled
                        await self._pause_gate.wait()
                        output.write(chunk)
                        downloaded += len(chunk)
                        self._update_progress(job, completed_base, downloaded, total, None, None)
            os.replace(part, target)
            job.phase = f"Verifying attachment {attachment.index}"
            self.database.save_job(job)
            try:
                return target, await verify_media(target, attachment)
            except Exception:
                with suppress(OSError):
                    target.unlink()
                raise

    async def _download_video(
        self,
        job: Job,
        analysis: Analysis,
        attachment: Attachment,
        quality: QualityOption | None,
        destination: Path,
        completed_base: int,
        *,
        target_base: Path | None = None,
    ) -> tuple[Path, int]:
        base = target_base or destination / filename_base(analysis, attachment)
        with output_lock(base):
            return await self._download_video_locked(
                job, analysis, attachment, quality, destination, completed_base, base
            )

    async def _download_video_locked(
        self,
        job: Job,
        analysis: Analysis,
        attachment: Attachment,
        quality: QualityOption | None,
        destination: Path,
        completed_base: int,
        base: Path,
    ) -> tuple[Path, int]:
        existing = [
            path
            for path in destination.glob(f"{base.name}.*")
            if ".part" not in path.name and not path.name.endswith(".ytdl") and path.is_file()
        ]
        if existing:
            job.phase = f"Verifying existing attachment {attachment.index}"
            self.database.save_job(job)
            return existing[0], await verify_media(existing[0], attachment, quality)
        selector = quality.selector if quality else "bv*+ba/b"
        settings = self.database.get_settings()
        command = [
            sys.executable,
            "-m",
            "yt_dlp",
            "--ignore-config",
            *ytdlp_progress_arguments(),
            "--continue",
            "--part",
            "--no-overwrites",
            "--retries",
            "10",
            "--fragment-retries",
            "10",
            "--concurrent-fragments",
            str(settings.concurrent_fragments),
            "--merge-output-format",
            "mp4",
            "--format",
            selector,
            "--output",
            f"{base}.%(ext)s",
            "--print",
            "after_move:XMD_FILE:%(filepath)s",
        ]
        ytdlp_attachments = [
            item
            for item in analysis.attachments
            if item.media_type != MediaType.PHOTO
            and item.role != AttachmentRole.ARTICLE_VIDEO
        ]
        if len(ytdlp_attachments) > 1:
            ordinal = next(
                index
                for index, item in enumerate(ytdlp_attachments, 1)
                if item.id == attachment.id
            )
            command.extend(["--playlist-items", str(ordinal)])
        command.append(analysis.url)
        self._active_process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        job.worker_pid = self._active_process.pid
        job.worker_pgid = os.getpgid(self._active_process.pid)
        job.heartbeat_at = utc_now()
        self.database.save_job(job)
        assert self._active_process.stdout
        final_path: Path | None = None
        recent: list[str] = []
        async for raw_line in self._active_process.stdout:
            line = raw_line.decode(errors="replace").strip()
            if line.startswith("XMD_PROGRESS:"):
                values = line.removeprefix("XMD_PROGRESS:").split("|")
                downloaded = _number(values, 0, int) or 0
                total = _number(values, 1, int)
                speed = _number(values, 2, float)
                eta = _number(values, 3, int)
                self._update_progress(job, completed_base, downloaded, total, speed, eta)
            elif line.startswith("XMD_FILE:"):
                final_path = Path(line.removeprefix("XMD_FILE:"))
            elif line:
                recent.append(line)
                recent = recent[-8:]
        return_code = await self._active_process.wait()
        self._active_process = None
        if self._cancel_requested:
            raise DownloadCancelled
        if return_code != 0:
            raise QueueError(recent[-1][-300:] if recent else "The video downloader failed.")
        if not final_path or not final_path.exists():
            matches = [
                path
                for path in destination.glob(f"{base.name}.*")
                if path.is_file() and not path.name.endswith(".part")
            ]
            final_path = matches[0] if matches else None
        if not final_path:
            raise QueueError("The downloader finished without producing a media file.")
        job.phase = f"Verifying attachment {attachment.index}"
        self.database.save_job(job)
        try:
            return final_path, await verify_media(final_path, attachment, quality)
        except Exception:
            with suppress(OSError):
                final_path.unlink()
            raise

    def _update_progress(
        self,
        job: Job,
        completed_base: int,
        current: int,
        current_total: int | None,
        speed: float | None,
        eta: int | None,
    ) -> None:
        job.status = JobStatus.RUNNING
        if job.current_attachment is not None:
            job.phase = f"Downloading attachment {job.current_attachment}"
        job.downloaded_bytes = completed_base + current
        if job.total_steps:
            fraction = current / current_total if current_total else 0
            job.progress = min(
                99.9, (job.completed_steps + min(1, fraction)) / job.total_steps * 100
            )
        else:
            denominator = job.total_bytes or (
                (completed_base + current_total) if current_total else None
            )
            if denominator:
                job.progress = min(99.9, job.downloaded_bytes / denominator * 100)
        job.speed = speed
        job.eta = eta
        job.heartbeat_at = utc_now()
        now = time.monotonic()
        if (
            now - self._last_progress_save < 0.25
            and abs(job.progress - self._last_saved_progress) < 0.5
        ):
            return
        self._last_progress_save = now
        self._last_saved_progress = job.progress
        self.database.save_job(job)

    async def _stop_process(self, sig: signal.Signals) -> None:
        process = self._active_process
        if not process or process.returncode is not None:
            return
        with suppress(ProcessLookupError):
            os.killpg(process.pid, sig)
        try:
            await asyncio.wait_for(process.wait(), timeout=4)
        except TimeoutError:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), timeout=2)
            except TimeoutError:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                await process.wait()


def _number(values: list[str], index: int, cast):
    if index >= len(values) or values[index] in {"", "NA", "None", "null"}:
        return None
    try:
        return cast(float(values[index])) if cast is int else cast(values[index])
    except (TypeError, ValueError):
        return None
