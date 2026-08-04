from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class MediaType(StrEnum):
    PHOTO = "photo"
    VIDEO = "video"
    GIF = "gif"


class OutputFormat(StrEnum):
    MEDIA = "media"
    MARKDOWN = "markdown"
    PDF = "pdf"


class ContentKind(StrEnum):
    POST = "post"
    ARTICLE = "article"


class AttachmentRole(StrEnum):
    POST_MEDIA = "post_media"
    ARTICLE_COVER = "article_cover"
    ARTICLE_IMAGE = "article_image"
    ARTICLE_VIDEO = "article_video"


class QualityOption(BaseModel):
    id: str
    label: str
    selector: str
    width: int | None = None
    height: int | None = None
    size_bytes: int | None = None
    size_is_estimate: bool = False
    protocol: str | None = None


class Attachment(BaseModel):
    id: str
    index: int
    media_type: MediaType
    extension: str
    role: AttachmentRole = AttachmentRole.POST_MEDIA
    source_id: str | None = None
    alt_text: str | None = None
    width: int | None = None
    height: int | None = None
    duration: float | None = None
    size_bytes: int | None = None
    size_is_estimate: bool = False
    video_ordinal: int | None = None
    qualities: list[QualityOption] = Field(default_factory=list)


class ArticleMetadata(BaseModel):
    id: str
    title: str
    published_at: str | None = None
    updated_at: str | None = None
    html: str = ""
    html_renderer_version: int = 0


class PostMetadata(BaseModel):
    post_id: str
    author_name: str
    author_handle: str
    text: str = ""
    posted_at: str | None = None


class Analysis(BaseModel):
    id: str
    url: str
    post: PostMetadata
    attachments: list[Attachment]
    content_kind: ContentKind = ContentKind.POST
    article: ArticleMetadata | None = None
    output_relative_dir: str | None = None
    created_at: str = Field(default_factory=utc_now)


class AnalyzeRequest(BaseModel):
    url: str = Field(min_length=10, max_length=500)


class AttachmentSelection(BaseModel):
    attachment_id: str
    quality_id: str | None = None


class CreateJobRequest(BaseModel):
    analysis_id: str
    selections: list[AttachmentSelection] = Field(default_factory=list)
    outputs: list[OutputFormat] = Field(default_factory=lambda: [OutputFormat.MEDIA])
    include_document_media: bool = True
    destination: str | None = None


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RevealTarget(StrEnum):
    OUTPUT_FOLDER = "output_folder"
    COMPLETED_FILE = "completed_file"


class RevealRequest(BaseModel):
    model_config = {"extra": "forbid"}

    target: RevealTarget
    completed_file_index: int | None = Field(default=None, ge=0, strict=True)


class Job(BaseModel):
    id: str
    analysis_id: str
    url: str
    post: PostMetadata
    selections: list[AttachmentSelection]
    destination: str
    content_kind: ContentKind = ContentKind.POST
    article: ArticleMetadata | None = None
    layout_version: int = 1
    output_dir: str | None = None
    outputs: list[OutputFormat] = Field(default_factory=lambda: [OutputFormat.MEDIA])
    include_document_media: bool = True
    status: JobStatus = JobStatus.QUEUED
    phase: str = "Queued"
    progress: float = 0
    downloaded_bytes: int = 0
    total_bytes: int | None = None
    speed: float | None = None
    eta: int | None = None
    current_attachment: int | None = None
    total_steps: int = 0
    completed_steps: int = 0
    completed_files: list[str] = Field(default_factory=list)
    worker_pid: int | None = None
    worker_pgid: int | None = None
    heartbeat_at: str | None = None
    error: str | None = None
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)


class Settings(BaseModel):
    download_dir: str
    concurrent_fragments: int = Field(default=4, ge=1, le=8)


class SettingsPatch(BaseModel):
    download_dir: str | None = None
    concurrent_fragments: int | None = Field(default=None, ge=1, le=8)


class Health(BaseModel):
    ok: Literal[True] = True
    ffmpeg: bool
    ffprobe: bool
    gallery_dl: bool
    yt_dlp: bool
    queue_running: bool
