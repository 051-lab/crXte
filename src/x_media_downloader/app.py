from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .config import STATIC_DIR, ffmpeg_available, ffprobe_available
from .database import Database
from .documents import DocumentError, render_markdown, render_pdf
from .extractors import AnalysisError, analyze_url
from .layout import LayoutError, build_export_layout, media_filename, post_relative_dir
from .models import (
    Analysis,
    AnalyzeRequest,
    CreateJobRequest,
    Health,
    Job,
    JobStatus,
    MediaType,
    OutputFormat,
    RevealRequest,
    RevealTarget,
    Settings,
    SettingsPatch,
)
from .queue import DownloadQueue, QueueError, estimated_total, selected_quality
from .verification import VerificationError, verify_markdown, verify_media, verify_pdf

database = Database()
queue = DownloadQueue(database)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await queue.start()
    yield
    await queue.stop()
    database.close()


app = FastAPI(title="crXte", version="0.3.0", lifespan=lifespan)


@app.middleware("http")
async def local_only(request: Request, call_next):
    host = (request.url.hostname or "").lower()
    if host not in {"127.0.0.1", "localhost", "testserver"}:
        return _error_response(403, "This app only accepts local requests.")
    origin = request.headers.get("origin")
    if origin:
        origin_host = (urlsplit(origin).hostname or "").lower()
        if origin_host not in {"127.0.0.1", "localhost", "testserver"}:
            return _error_response(403, "Cross-origin requests are not allowed.")
    return await call_next(request)


def _error_response(status: int, detail: str):
    from fastapi.responses import JSONResponse

    return JSONResponse({"detail": detail}, status_code=status)


@app.get("/api/health", response_model=Health)
async def health() -> Health:
    return Health(
        ffmpeg=ffmpeg_available(),
        ffprobe=ffprobe_available(),
        gallery_dl=shutil.which("gallery-dl") is not None or _module_available("gallery_dl"),
        yt_dlp=shutil.which("yt-dlp") is not None or _module_available("yt_dlp"),
        queue_running=queue.is_running,
    )


def _module_available(name: str) -> bool:
    import importlib.util

    return importlib.util.find_spec(name) is not None


class ExplorerIntegrationError(Exception):
    pass


def _resolve_reveal_directory(path: str, label: str) -> Path:
    try:
        resolved = Path(path).expanduser().resolve(strict=True)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=f"{label} does not exist.") from error
    except (OSError, RuntimeError) as error:
        raise HTTPException(status_code=422, detail=f"{label} is not accessible.") from error
    if not resolved.is_dir():
        raise HTTPException(status_code=422, detail=f"{label} must be a folder.")
    return resolved


def _resolve_reveal_file(path: str) -> Path:
    try:
        resolved = Path(path).expanduser().resolve(strict=True)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail="Completed file does not exist.") from error
    except (OSError, RuntimeError) as error:
        raise HTTPException(status_code=422, detail="Completed file is not accessible.") from error
    if not resolved.is_file():
        raise HTTPException(status_code=422, detail="Completed file must be a file.")
    return resolved


def _require_reveal_containment(path: Path, root: Path, detail: str) -> None:
    if not path.is_relative_to(root):
        raise HTTPException(status_code=422, detail=detail)


def _reveal_path(job: Job, settings: Settings, body: RevealRequest) -> tuple[Path, bool]:
    if body.target == RevealTarget.OUTPUT_FOLDER:
        if body.completed_file_index is not None:
            raise HTTPException(
                status_code=422,
                detail="completed_file_index is only valid for a completed file.",
            )
    elif body.completed_file_index is None:
        raise HTTPException(
            status_code=422,
            detail="completed_file_index is required for a completed file.",
        )
    elif body.completed_file_index >= len(job.completed_files):
        raise HTTPException(
            status_code=422,
            detail="completed_file_index does not identify a completed file.",
        )

    configured_root = _resolve_reveal_directory(
        settings.download_dir, "Configured download folder"
    )
    job_root = _resolve_reveal_directory(
        job.output_dir or job.destination, "Job output folder"
    )
    _require_reveal_containment(
        job_root,
        configured_root,
        "Job output folder is outside the configured download folder.",
    )

    if body.target == RevealTarget.OUTPUT_FOLDER:
        target = job_root
        is_file = False
    else:
        target = _resolve_reveal_file(job.completed_files[body.completed_file_index])
        is_file = True

    _require_reveal_containment(
        target,
        configured_root,
        "Reveal target is outside the configured download folder.",
    )
    _require_reveal_containment(
        target,
        job_root,
        "Reveal target is outside the job output folder.",
    )
    return target, is_file


def _windows_path(path: Path) -> str:
    try:
        result = subprocess.run(
            ["wslpath", "-w", str(path)],
            check=True,
            capture_output=True,
            text=True,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ExplorerIntegrationError from error
    windows_path = result.stdout.strip()
    if not windows_path:
        raise ExplorerIntegrationError
    return windows_path


def _launch_explorer(path: Path, *, select_file: bool) -> None:
    windows_path = _windows_path(path)
    arguments = ["explorer.exe", windows_path]
    if select_file:
        arguments = ["explorer.exe", "/select,", windows_path]
    try:
        subprocess.Popen(
            arguments,
            shell=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ExplorerIntegrationError from error


def _reveal_job_target(job: Job, settings: Settings, body: RevealRequest) -> None:
    path, select_file = _reveal_path(job, settings, body)
    _launch_explorer(path, select_file=select_file)


@app.post("/api/analyze", response_model=Analysis)
async def analyze(body: AnalyzeRequest) -> Analysis:
    try:
        analysis = await analyze_url(body.url)
    except AnalysisError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    analysis.output_relative_dir = post_relative_dir(analysis.post).as_posix()
    database.save_analysis(analysis)
    return analysis


@app.post("/api/jobs", response_model=Job, status_code=201)
async def create_job(body: CreateJobRequest) -> Job:
    analysis = database.get_analysis(body.analysis_id)
    if not analysis:
        raise HTTPException(status_code=404, detail="Analysis not found. Paste the link again.")
    _validate_selections(analysis, body.selections, body.outputs)
    destination = _validated_destination(body.destination or database.get_settings().download_dir)
    try:
        layout = build_export_layout(destination, analysis)
    except LayoutError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    selection_key = _selection_key(
        analysis, body.selections, body.outputs, body.include_document_media
    )
    for existing in database.list_jobs(500):
        if (
            existing.layout_version != 2
            or existing.url != analysis.url
            or existing.output_dir != str(layout.output_dir)
        ):
            continue
        existing_analysis = database.get_analysis(existing.analysis_id)
        if (
            not existing_analysis
            or _selection_key(
                existing_analysis,
                existing.selections,
                existing.outputs,
                existing.include_document_media,
            )
            != selection_key
        ):
            continue
        if existing.status in {JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.PAUSED}:
            raise HTTPException(
                status_code=409, detail="This export is already in the queue."
            )
        if existing.status in {JobStatus.FAILED, JobStatus.CANCELLED}:
            raise HTTPException(
                status_code=409, detail="This export already has a Retry action in the queue."
            )
        if existing.status == JobStatus.COMPLETED:
            if await _completed_job_is_reusable(existing, existing_analysis):
                raise HTTPException(status_code=409, detail="This export already exists.")
            existing.status = JobStatus.FAILED
            existing.phase = "Completed output is missing or invalid"
            existing.error = "Retry this export to restore its expected files."
            database.save_job(existing)
            raise HTTPException(
                status_code=409, detail="This export already has a Retry action in the queue."
            )
    job = Job(
        id=uuid.uuid4().hex,
        analysis_id=analysis.id,
        url=analysis.url,
        post=analysis.post,
        content_kind=analysis.content_kind,
        article=analysis.article,
        selections=body.selections,
        outputs=body.outputs,
        include_document_media=body.include_document_media,
        destination=str(destination),
        layout_version=2,
        output_dir=str(layout.output_dir),
        total_bytes=estimated_total(analysis, body.selections),
    )
    database.save_job(job)
    queue.notify()
    return job


def _validate_selections(
    analysis: Analysis,
    selections,
    outputs: list[OutputFormat] | None = None,
) -> None:
    requested_outputs = outputs if outputs is not None else [OutputFormat.MEDIA]
    if not requested_outputs:
        raise HTTPException(status_code=422, detail="Select at least one output format.")
    if len(requested_outputs) != len(set(requested_outputs)):
        raise HTTPException(status_code=422, detail="An output format can only be selected once.")
    if OutputFormat.MEDIA in requested_outputs and not selections:
        raise HTTPException(
            status_code=422, detail="Select at least one attachment for media output."
        )
    attachment_lookup = {item.id: item for item in analysis.attachments}
    selection_ids = [item.attachment_id for item in selections]
    if len(selection_ids) != len(set(selection_ids)):
        raise HTTPException(status_code=422, detail="An attachment can only be selected once.")
    for selection in selections:
        attachment = attachment_lookup.get(selection.attachment_id)
        if not attachment:
            raise HTTPException(
                status_code=422, detail="One or more selected attachments are invalid."
            )
        if selection.quality_id and selection.quality_id not in {
            quality.id for quality in attachment.qualities
        }:
            raise HTTPException(
                status_code=422, detail="One or more selected qualities are invalid."
            )


def _selection_key(
    analysis: Analysis,
    selections,
    outputs: list[OutputFormat] | None = None,
    include_document_media: bool = True,
):
    attachment_lookup = {item.id: item for item in analysis.attachments}
    normalized = []
    for selection in selections:
        attachment = attachment_lookup.get(selection.attachment_id)
        quality = selected_quality(attachment, selection) if attachment else None
        normalized.append(
            (selection.attachment_id, quality.id if quality else None)
        )
    normalized_selections = tuple(sorted(normalized))
    if outputs is None:
        return normalized_selections
    return (
        tuple(sorted(output.value for output in outputs)),
        include_document_media,
        normalized_selections,
    )


async def _completed_job_is_reusable(job: Job, analysis: Analysis) -> bool:
    try:
        layout = build_export_layout(Path(job.destination), analysis)
        outputs = set(job.outputs)
        wants_documents = bool(outputs & {OutputFormat.MARKDOWN, OutputFormat.PDF})
        if (
            wants_documents
            and analysis.article
            and analysis.article.html_renderer_version < 1
        ):
            return False
        needs_media = OutputFormat.MEDIA in outputs or (
            wants_documents and job.include_document_media
        )
        attachment_lookup = {item.id: item for item in analysis.attachments}
        media_paths: dict[str, Path] = {}
        if needs_media:
            for selection in job.selections:
                attachment = attachment_lookup.get(selection.attachment_id)
                if not attachment:
                    return False
                quality = selected_quality(attachment, selection)
                target = layout.media_dir / media_filename(attachment, quality)
                if not target.is_file() and attachment.media_type != MediaType.PHOTO:
                    candidates = [
                        path
                        for path in layout.media_dir.glob(f"{target.stem}.*")
                        if path.is_file()
                        and ".part" not in path.name
                        and not path.name.endswith(".ytdl")
                    ]
                    if len(candidates) != 1:
                        return False
                    target = candidates[0]
                await verify_media(target, attachment, quality)
                media_paths[attachment.id] = target
        document_media = media_paths if job.include_document_media else {}
        if OutputFormat.MARKDOWN in outputs:
            content = await asyncio.to_thread(render_markdown, analysis, document_media)
            await asyncio.to_thread(
                verify_markdown,
                layout.markdown_path,
                content,
                list(document_media.values()),
            )
        if OutputFormat.PDF in outputs:
            content = await asyncio.to_thread(render_pdf, analysis, document_media)
            await asyncio.to_thread(verify_pdf, layout.pdf_path, content)
        return True
    except (DocumentError, LayoutError, OSError, VerificationError):
        return False


@app.get("/api/jobs", response_model=list[Job])
async def list_jobs() -> list[Job]:
    return database.list_jobs()


@app.get("/api/jobs/{job_id}", response_model=Job)
async def get_job(job_id: str) -> Job:
    job = database.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Download job not found.")
    return job


@app.post("/api/jobs/{job_id}/reveal")
async def reveal_job(job_id: str, body: RevealRequest) -> dict[str, bool]:
    job = database.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Download job not found.")
    settings = database.get_settings()
    try:
        await asyncio.to_thread(_reveal_job_target, job, settings, body)
    except ExplorerIntegrationError as error:
        raise HTTPException(
            status_code=503,
            detail="Windows Explorer integration is unavailable.",
        ) from error
    return {"ok": True}


@app.post("/api/jobs/{job_id}/{action}", response_model=Job)
async def control_job(job_id: str, action: str) -> Job:
    actions = {
        "pause": queue.pause,
        "resume": queue.resume,
        "cancel": queue.cancel,
        "retry": queue.retry,
    }
    if action not in actions:
        raise HTTPException(status_code=404, detail="Unknown job action.")
    try:
        return await actions[action](job_id)
    except QueueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.get("/api/settings", response_model=Settings)
async def get_settings() -> Settings:
    return database.get_settings()


@app.patch("/api/settings", response_model=Settings)
async def patch_settings(body: SettingsPatch) -> Settings:
    current = database.get_settings()
    values = current.model_dump()
    if body.download_dir is not None:
        values["download_dir"] = str(_validated_destination(body.download_dir, create=False))
    if body.concurrent_fragments is not None:
        values["concurrent_fragments"] = body.concurrent_fragments
    settings = Settings(**values)
    database.save_settings(settings)
    return settings


def _validated_destination(value: str, *, create: bool = True) -> Path:
    if not value.strip():
        raise HTTPException(status_code=422, detail="Download folder cannot be empty.")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise HTTPException(status_code=422, detail="Download folder must be an absolute path.")
    path = path.resolve()
    try:
        if create:
            path.mkdir(parents=True, exist_ok=True)
        parent = path if path.exists() else path.parent
        if not parent.exists():
            raise OSError
    except OSError as error:
        raise HTTPException(status_code=422, detail="Download folder is not accessible.") from error
    return path


@app.get("/api/events")
async def events(request: Request):
    async def stream():
        last_payload = ""
        while not await request.is_disconnected():
            payload = json.dumps(
                [job.model_dump(mode="json") for job in database.list_jobs()],
                separators=(",", ":"),
            )
            if payload != last_payload:
                yield f"event: jobs\ndata: {payload}\n\n"
                last_payload = payload
            else:
                yield ": keepalive\n\n"
            await asyncio.sleep(0.75)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
