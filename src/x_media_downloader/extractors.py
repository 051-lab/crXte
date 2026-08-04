from __future__ import annotations

import asyncio
import json
import re
import sys
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import httpx

from .models import (
    Analysis,
    ArticleMetadata,
    Attachment,
    AttachmentRole,
    ContentKind,
    MediaType,
    PostMetadata,
    QualityOption,
)

ALLOWED_X_HOSTS = {
    "x.com",
    "www.x.com",
    "mobile.x.com",
    "twitter.com",
    "www.twitter.com",
    "mobile.twitter.com",
}
STATUS_PATH = re.compile(r"^/(?:i/web/|[^/]+/)?status/(\d+)(?:/.*)?$")
TWIMG_HOSTS = {"pbs.twimg.com", "video.twimg.com"}


class AnalysisError(RuntimeError):
    pass


@dataclass(slots=True)
class ResolvedMedia:
    index: int
    url: str
    metadata: dict


def normalize_x_url(value: str) -> tuple[str, str]:
    value = value.strip()
    try:
        parsed = urlsplit(value)
    except ValueError as error:
        raise AnalysisError("That is not a valid URL.") from error
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or host not in ALLOWED_X_HOSTS:
        raise AnalysisError("Paste a public x.com or twitter.com post URL.")
    match = STATUS_PATH.match(parsed.path)
    if not match:
        raise AnalysisError("The URL must point to an individual X post.")
    post_id = match.group(1)
    normalized = urlunsplit(("https", "x.com", f"/i/web/status/{post_id}", "", ""))
    return normalized, post_id


async def _run_json(command: list[str], label: str, timeout: float = 90) -> object:
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError as error:
        if "process" in locals():
            process.kill()
            await process.wait()
        raise AnalysisError(f"{label} timed out while contacting X.") from error
    if process.returncode != 0:
        detail = stderr.decode(errors="replace").strip().splitlines()
        message = detail[-1] if detail else f"{label} could not read this post."
        raise AnalysisError(_friendly_extractor_error(message))
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as error:
        raise AnalysisError(f"{label} returned unreadable metadata.") from error


def _friendly_extractor_error(message: str) -> str:
    lowered = message.lower()
    if "not found" in lowered or "does not exist" in lowered:
        return "This post could not be found or has been deleted."
    if "login" in lowered or "private" in lowered or "protected" in lowered:
        return "This post is not publicly accessible. crXte does not use login cookies."
    if "rate" in lowered or "429" in lowered:
        return "X is rate-limiting requests. Wait a few minutes and try again."
    return message[-300:]


def gallery_command(url: str, *, dump_json: bool = True) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "x_media_downloader.gallery_runner",
        "--config-ignore",
        "--no-input",
        "--no-colors",
        "-o",
        "extractor.twitter.tweet-endpoint=restid",
        "-o",
        "extractor.twitter.quoted=false",
        "-o",
        "extractor.twitter.conversations=false",
        "-o",
        "extractor.twitter.replies=false",
        "-o",
        "extractor.twitter.size=[\"orig\",\"4096x4096\",\"large\"]",
        "-o",
        "extractor.twitter.text-tweets=true",
        "-o",
        "extractor.twitter.articles=[\"meta\",\"html\",\"cover\",\"media\"]",
    ]
    if dump_json:
        command.append("--dump-json")
    command.append(url)
    return command


def ytdlp_command(url: str) -> list[str]:
    return [
        sys.executable,
        "-m",
        "yt_dlp",
        "--ignore-config",
        "--no-warnings",
        "--simulate",
        "--dump-single-json",
        url,
    ]


async def resolve_gallery_media(url: str) -> tuple[dict, list[ResolvedMedia]]:
    payload = await _run_json(gallery_command(url), "The X media extractor")
    if not isinstance(payload, list):
        raise AnalysisError("X returned an unexpected media response.")
    post: dict = {}
    resolved: list[ResolvedMedia] = []
    for message in payload:
        if not isinstance(message, list) or not message:
            continue
        if message[0] == 2 and len(message) > 1 and isinstance(message[1], dict):
            post = message[1]
        elif message[0] == 3 and len(message) > 2 and isinstance(message[2], dict):
            metadata = message[2]
            index = int(metadata.get("num") or len(resolved) + 1)
            resolved.append(ResolvedMedia(index=index, url=str(message[1]), metadata=metadata))
    return post, sorted(resolved, key=lambda item: item.index)


async def resolve_ytdlp_entries(url: str) -> list[dict]:
    try:
        payload = await _run_json(ytdlp_command(url), "The video extractor")
    except AnalysisError:
        return []
    if not isinstance(payload, dict):
        return []
    entries = payload.get("entries")
    if isinstance(entries, list):
        return [entry for entry in entries if isinstance(entry, dict)]
    return [payload]


async def _remote_size(url: str) -> int | None:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if host not in TWIMG_HOSTS or parsed.path.lower().endswith(".m3u8"):
        return None
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        async with httpx.AsyncClient(follow_redirects=False, timeout=12, headers=headers) as client:
            response = await client.head(url)
            if response.status_code < 400 and response.headers.get("content-length"):
                return int(response.headers["content-length"])
            response = await client.get(url, headers={**headers, "Range": "bytes=0-0"})
            content_range = response.headers.get("content-range", "")
            if "/" in content_range:
                return int(content_range.rsplit("/", 1)[1])
    except (httpx.HTTPError, ValueError):
        return None
    return None


def _author_fields(metadata: dict) -> tuple[str, str]:
    author = metadata.get("author") if isinstance(metadata.get("author"), dict) else {}
    handle = str(
        author.get("name") or author.get("screen_name") or metadata.get("user") or "unknown"
    )
    name = str(author.get("nick") or handle)
    return name, handle.lstrip("@")


def _media_type(metadata: dict) -> MediaType:
    value = str(metadata.get("type") or "").lower()
    image_extensions = {"jpg", "jpeg", "png", "webp"}
    if (
        value in {"photo", "article:cover", "article:image"}
        or str(metadata.get("extension", "")).lower() in image_extensions
    ):
        return MediaType.PHOTO
    if value in {"gif", "animated_gif", "animated"}:
        return MediaType.GIF
    return MediaType.VIDEO


def _attachment_role(metadata: dict) -> AttachmentRole:
    return {
        "article:cover": AttachmentRole.ARTICLE_COVER,
        "article:image": AttachmentRole.ARTICLE_IMAGE,
        "article:video": AttachmentRole.ARTICLE_VIDEO,
    }.get(str(metadata.get("type") or "").lower(), AttachmentRole.POST_MEDIA)


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _source_id(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return None


def _article_id(value: object, post_id: str) -> str:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return post_id


def _article_metadata(post: dict, post_id: str) -> ArticleMetadata | None:
    article = post.get("article")
    if not isinstance(article, dict):
        return None
    return ArticleMetadata(
        id=_article_id(article.get("id"), post_id),
        title=_optional_text(article.get("title")) or "",
        published_at=_optional_text(article.get("date")),
        updated_at=_optional_text(article.get("date_updated")),
        html=_optional_text(article.get("html")) or "",
        html_renderer_version=1,
    )


def _video_formats(entry: dict) -> list[dict]:
    formats = entry.get("formats")
    if not isinstance(formats, list):
        return []
    return [
        item
        for item in formats
        if isinstance(item, dict)
        and item.get("height")
        and item.get("vcodec") != "none"
    ]


def _pick_formats_by_height(entry: dict) -> list[dict]:
    grouped: dict[int, list[dict]] = {}
    for item in _video_formats(entry):
        grouped.setdefault(int(item["height"]), []).append(item)
    selected: list[dict] = []
    for height, candidates in grouped.items():
        candidates.sort(
            key=lambda item: (
                item.get("acodec") not in {None, "none"},
                str(item.get("protocol", "")).startswith("http"),
                float(item.get("tbr") or 0),
            ),
            reverse=True,
        )
        selected.append({**candidates[0], "height": height})
    return sorted(selected, key=lambda item: int(item["height"]), reverse=True)


async def quality_options(entry: dict) -> list[QualityOption]:
    picked = _pick_formats_by_height(entry)
    sizes = await asyncio.gather(
        *[_remote_size(str(item.get("url") or "")) for item in picked]
    ) if picked else []
    options: list[QualityOption] = []
    for position, (item, exact_size) in enumerate(zip(picked, sizes, strict=True)):
        height = int(item["height"])
        format_id = str(item.get("format_id") or f"height-{height}")
        fallback = f"bv*[height<={height}]+ba/b[height<={height}]"
        selector = f"{format_id}/{fallback}"
        raw_size = item.get("filesize") or item.get("filesize_approx")
        size = exact_size or (int(raw_size) if raw_size else None)
        label = f"{height}p"
        if position == 0:
            label += " · Best"
        options.append(
            QualityOption(
                id=f"q-{height}",
                label=label,
                selector=selector,
                width=int(item["width"]) if item.get("width") else None,
                height=height,
                size_bytes=size,
                size_is_estimate=exact_size is None and size is not None,
                protocol=str(item.get("protocol") or ""),
            )
        )
    if not options:
        options.append(
            QualityOption(
                id="q-best",
                label="Best available",
                selector="bv*+ba/b",
                height=entry.get("height"),
                width=entry.get("width"),
                size_bytes=entry.get("filesize") or entry.get("filesize_approx"),
                size_is_estimate=bool(entry.get("filesize_approx") and not entry.get("filesize")),
            )
        )
    return options


async def analyze_url(value: str) -> Analysis:
    url, post_id = normalize_x_url(value)
    gallery_result, yt_result = await asyncio.gather(
        resolve_gallery_media(url),
        resolve_ytdlp_entries(url),
        return_exceptions=True,
    )
    gallery_error = gallery_result if isinstance(gallery_result, AnalysisError) else None
    if isinstance(gallery_result, BaseException):
        gallery_post, media = {}, []
    else:
        gallery_post, media = gallery_result
    yt_entries = [] if isinstance(yt_result, BaseException) else yt_result
    name, handle = _author_fields(gallery_post)
    if not gallery_post and yt_entries:
        first_entry = yt_entries[0]
        handle = str(first_entry.get("uploader_id") or first_entry.get("channel_id") or "unknown")
        name = str(first_entry.get("uploader") or first_entry.get("channel") or handle)
    article = _article_metadata(gallery_post, post_id)
    post = PostMetadata(
        post_id=post_id,
        author_name=name,
        author_handle=handle,
        text=str(
            gallery_post.get("content")
            or (yt_entries[0].get("description") if yt_entries else "")
            or ""
        ),
        posted_at=str(gallery_post.get("date") or "") or None,
    )
    content_kind = ContentKind.ARTICLE if article else ContentKind.POST

    attachments: list[Attachment] = []
    video_ordinal = 0
    yt_cursor = 0
    remote_sizes = await asyncio.gather(*(_remote_size(item.url) for item in media))
    for item, remote_size in zip(media, remote_sizes, strict=True):
        metadata = item.metadata
        media_type = _media_type(metadata)
        qualities: list[QualityOption] = []
        ordinal: int | None = None
        if media_type != MediaType.PHOTO:
            video_ordinal += 1
            ordinal = video_ordinal
            if yt_cursor < len(yt_entries):
                qualities = await quality_options(yt_entries[yt_cursor])
                yt_cursor += 1
        attachments.append(
            Attachment(
                id=f"a-{item.index}",
                index=item.index,
                source_id=_source_id(metadata.get("media_id")),
                media_type=media_type,
                extension=str(
                    metadata.get("extension")
                    or ("jpg" if media_type == MediaType.PHOTO else "mp4")
                ),
                role=_attachment_role(metadata),
                alt_text=(
                    _optional_text(metadata.get("description"))
                    or _optional_text(metadata.get("alt_text"))
                ),
                width=int(metadata["width"]) if metadata.get("width") else None,
                height=int(metadata["height"]) if metadata.get("height") else None,
                duration=float(metadata["duration"]) if metadata.get("duration") else None,
                size_bytes=remote_size,
                video_ordinal=ordinal,
                qualities=qualities,
            )
        )

    while yt_cursor < len(yt_entries):
        entry = yt_entries[yt_cursor]
        index = len(attachments) + 1
        video_ordinal += 1
        attachments.append(
            Attachment(
                id=f"a-{index}",
                index=index,
                media_type=MediaType.VIDEO,
                extension=str(entry.get("ext") or "mp4"),
                width=entry.get("width"),
                height=entry.get("height"),
                duration=entry.get("duration"),
                video_ordinal=video_ordinal,
                qualities=await quality_options(entry),
            )
        )
        yt_cursor += 1

    if not (post.text.strip() or article or attachments):
        if gallery_error:
            raise gallery_error
        raise AnalysisError("The X media extractor could not read this post.")
    return Analysis(
        id=uuid.uuid4().hex,
        url=url,
        post=post,
        attachments=attachments,
        content_kind=content_kind,
        article=article,
    )


def find_resolved_item(items: Iterable[ResolvedMedia], index: int) -> ResolvedMedia | None:
    return next((item for item in items if item.index == index), None)
