from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

from pypdf import PdfReader

from .models import Attachment, MediaType, QualityOption


class VerificationError(RuntimeError):
    pass


def _close_enough(actual: int, expected: int) -> bool:
    tolerance = max(64 * 1024, int(expected * 0.001))
    return abs(actual - expected) <= tolerance


async def verify_media(
    path: Path,
    attachment: Attachment,
    quality: QualityOption | None = None,
) -> int:
    if not path.is_file():
        raise VerificationError("The completed output file is missing.")
    actual_size = path.stat().st_size
    expected_size = quality.size_bytes if quality else attachment.size_bytes
    size_is_estimate = quality.size_is_estimate if quality else attachment.size_is_estimate
    if expected_size and not size_is_estimate and not _close_enough(actual_size, expected_size):
        raise VerificationError(
            f"Existing file size is {actual_size:,} bytes; expected {expected_size:,} bytes. "
            "Move or rename the existing file before retrying."
        )

    process = await asyncio.create_subprocess_exec(
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration:stream=codec_type,width,height",
        "-of",
        "json",
        str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
    except TimeoutError as error:
        process.kill()
        await process.wait()
        raise VerificationError("ffprobe timed out while verifying the output.") from error
    if process.returncode != 0:
        detail = stderr.decode(errors="replace").strip().splitlines()
        message = detail[-1][-240:] if detail else "ffprobe rejected the output file."
        raise VerificationError(message)
    try:
        probe = json.loads(stdout)
    except json.JSONDecodeError as error:
        raise VerificationError("ffprobe returned unreadable verification data.") from error

    streams = [item for item in probe.get("streams", []) if item.get("codec_type") == "video"]
    if not streams:
        raise VerificationError("The output does not contain a readable image or video stream.")
    stream = streams[0]
    expected_height = quality.height if quality and quality.height else attachment.height
    if expected_height and stream.get("height") and int(stream["height"]) != expected_height:
        raise VerificationError(
            f"Output height is {stream['height']}p; expected {expected_height}p."
        )
    if attachment.media_type != MediaType.PHOTO and attachment.duration:
        try:
            duration = float(probe.get("format", {}).get("duration"))
        except (TypeError, ValueError) as error:
            raise VerificationError("The output duration could not be verified.") from error
        tolerance = max(2.0, attachment.duration * 0.005)
        if abs(duration - attachment.duration) > tolerance:
            raise VerificationError(
                f"Output duration is {duration:.2f}s; expected about {attachment.duration:.2f}s."
            )
    return actual_size


_MARKDOWN_TARGET = re.compile(r"!?\[[^\]]*\]\((?:<)?([^)>]+)")
_UNSAFE_HTML = re.compile(r"<\s*/?\s*(?:script|iframe|object|embed|svg)\b", re.IGNORECASE)


def _verify_document_file(path: Path) -> int:
    if not path.is_file():
        raise VerificationError("The completed document is missing.")
    size = path.stat().st_size
    if not size:
        raise VerificationError("The completed document is empty.")
    return size


def verify_markdown(
    path: Path,
    expected_content: bytes | None = None,
    referenced_assets: list[Path] | None = None,
) -> int:
    size = _verify_document_file(path)
    raw_content = path.read_bytes()
    if expected_content is not None and raw_content != expected_content:
        raise VerificationError("The existing Markdown document differs from this export.")
    try:
        content = raw_content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise VerificationError("The Markdown document is not valid UTF-8.") from error
    if not content.strip():
        raise VerificationError("The Markdown document has no readable content.")
    if "\x00" in content or _UNSAFE_HTML.search(content):
        raise VerificationError("The Markdown document contains unsafe raw content.")
    for match in _MARKDOWN_TARGET.finditer(content):
        target = match.group(1).strip()
        parsed = urlsplit(target)
        if parsed.scheme and parsed.scheme.lower() not in {"http", "https"}:
            raise VerificationError("The Markdown document contains an unsafe link target.")
        if not parsed.scheme:
            local_path = unquote(parsed.path)
            parts = PurePosixPath(local_path.replace("\\", "/")).parts
            if parsed.netloc or local_path.startswith(("/", "\\")) or ".." in parts:
                raise VerificationError("The Markdown document contains an unsafe link target.")
    for asset in referenced_assets or []:
        if not asset.is_file():
            raise VerificationError("A Markdown document asset is missing.")
    return size


def verify_pdf(path: Path, expected_content: bytes | None = None) -> int:
    size = _verify_document_file(path)
    if expected_content is not None and path.read_bytes() != expected_content:
        raise VerificationError("The existing PDF document differs from this export.")
    try:
        reader = PdfReader(path)
        if reader.is_encrypted or not reader.pages:
            raise VerificationError("The PDF document has no readable pages.")
        if not any((page.extract_text() or "").strip() for page in reader.pages):
            raise VerificationError("The PDF document has no readable text.")
    except VerificationError:
        raise
    except Exception as error:
        raise VerificationError("The PDF document could not be read.") from error
    return size
