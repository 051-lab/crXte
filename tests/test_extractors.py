from __future__ import annotations

import asyncio

import pytest

from x_media_downloader import extractors
from x_media_downloader.extractors import (
    AnalysisError,
    ResolvedMedia,
    analyze_url,
    gallery_command,
    normalize_x_url,
    quality_options,
)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("https://x.com/Raytar/status/2078098073901834651", "2078098073901834651"),
        ("https://twitter.com/i/web/status/12345?ref=home", "12345"),
        ("https://mobile.x.com/user/status/88/video/1", "88"),
    ],
)
def test_normalize_x_url(source: str, expected: str) -> None:
    url, post_id = normalize_x_url(source)
    assert url == f"https://x.com/i/web/status/{expected}"
    assert post_id == expected


@pytest.mark.parametrize(
    "source",
    [
        "http://x.com/user/status/123",
        "https://example.com/user/status/123",
        "https://x.com/user",
        "https://x.com/home?next=/user/status/123",
    ],
)
def test_normalize_rejects_non_public_status_urls(source: str) -> None:
    with pytest.raises(AnalysisError):
        normalize_x_url(source)


def test_gallery_command_ignores_global_config_and_cookies() -> None:
    command = gallery_command("https://x.com/i/web/status/1")
    assert command[1:3] == ["-m", "x_media_downloader.gallery_runner"]
    assert "--config-ignore" in command
    assert "--no-input" in command
    assert "--cookies" not in command
    assert "--cookies-from-browser" not in command
    assert "extractor.twitter.text-tweets=true" in command
    assert 'extractor.twitter.articles=["meta","html","cover","media"]' in command


@pytest.mark.asyncio
async def test_quality_options_collapse_formats_by_resolution(monkeypatch) -> None:
    async def fake_size(url: str) -> int | None:
        return {"https://video.twimg.com/1080.mp4": 1_059_845_532}.get(url)

    monkeypatch.setattr(extractors, "_remote_size", fake_size)
    entry = {
        "formats": [
            {
                "format_id": "hls-1080",
                "height": 1080,
                "width": 1920,
                "vcodec": "h264",
                "acodec": "none",
                "protocol": "m3u8_native",
                "tbr": 1120,
                "filesize_approx": 1_004_000_000,
                "url": "https://video.twimg.com/1080.m3u8",
            },
            {
                "format_id": "http-1080",
                "height": 1080,
                "width": 1920,
                "vcodec": "h264",
                "acodec": "aac",
                "protocol": "https",
                "tbr": 10000,
                "filesize_approx": 9_000_000_000,
                "url": "https://video.twimg.com/1080.mp4",
            },
            {
                "format_id": "http-720",
                "height": 720,
                "width": 1280,
                "vcodec": "h264",
                "acodec": "aac",
                "protocol": "https",
                "tbr": 2000,
                "filesize": 500_000_000,
                "url": "https://video.twimg.com/720.mp4",
            },
        ]
    }
    options = await quality_options(entry)
    assert [item.height for item in options] == [1080, 720]
    assert options[0].selector.startswith("http-1080/")
    assert options[0].size_bytes == 1_059_845_532
    assert options[0].size_is_estimate is False
    assert options[1].size_bytes == 500_000_000


@pytest.mark.asyncio
async def test_analyze_mixed_post_preserves_attachment_order(monkeypatch) -> None:
    post = {
        "author": {"name": "NASAJPL", "nick": "NASA JPL"},
        "content": "Launch success",
        "date": "2025-07-30T10:45:00",
    }
    media = [
        ResolvedMedia(
            1,
            "https://pbs.twimg.com/one.jpg",
            {
                "num": 1,
                "count": 3,
                "type": "photo",
                "extension": "jpg",
                "width": 4096,
                "height": 2731,
                "description": "Earth & Moon <together>",
            },
        ),
        ResolvedMedia(
            2,
            "https://video.twimg.com/two.mp4",
            {
                "num": 2,
                "count": 3,
                "type": "video",
                "extension": "mp4",
                "width": 1280,
                "height": 720,
                "duration": 66.594,
            },
        ),
        ResolvedMedia(
            3,
            "https://pbs.twimg.com/three.jpg",
            {
                "num": 3,
                "count": 3,
                "type": "photo",
                "extension": "jpg",
                "width": 4096,
                "height": 2730,
            },
        ),
    ]

    all_probes_started = asyncio.Event()
    probe_count = 0

    async def fake_gallery(_: str):
        return post, media

    async def fake_ytdlp(_: str):
        return [{"formats": [], "height": 720, "width": 1280, "duration": 66.594}]

    async def fake_size(_: str) -> int:
        nonlocal probe_count
        probe_count += 1
        if probe_count == len(media):
            all_probes_started.set()
        await asyncio.wait_for(all_probes_started.wait(), timeout=0.5)
        return 1234

    monkeypatch.setattr(extractors, "resolve_gallery_media", fake_gallery)
    monkeypatch.setattr(extractors, "resolve_ytdlp_entries", fake_ytdlp)
    monkeypatch.setattr(extractors, "_remote_size", fake_size)
    analysis = await analyze_url("https://x.com/NASAJPL/status/1950583566947213444")
    assert probe_count == len(media)
    assert analysis.post.author_handle == "NASAJPL"
    assert [item.media_type.value for item in analysis.attachments] == ["photo", "video", "photo"]
    assert [item.index for item in analysis.attachments] == [1, 2, 3]
    assert analysis.attachments[0].alt_text == "Earth & Moon <together>"
    assert analysis.attachments[1].video_ordinal == 1
    assert analysis.attachments[1].qualities[0].id == "q-best"


@pytest.mark.asyncio
async def test_analyze_text_only_post_without_attachments(monkeypatch) -> None:
    async def fake_gallery(_: str):
        return {
            "author": {"name": "writer", "nick": "Writer"},
            "content": "A post with no media",
            "date": "2026-07-26T12:00:00",
        }, []

    async def fake_ytdlp(_: str):
        return []

    monkeypatch.setattr(extractors, "resolve_gallery_media", fake_gallery)
    monkeypatch.setattr(extractors, "resolve_ytdlp_entries", fake_ytdlp)

    analysis = await analyze_url("https://x.com/writer/status/42")

    assert analysis.content_kind.value == "post"
    assert analysis.article is None
    assert analysis.attachments == []
    assert analysis.post.text == "A post with no media"


@pytest.mark.asyncio
async def test_analyze_rejects_metadata_without_content(monkeypatch) -> None:
    async def fake_gallery(_: str):
        return {"author": {"name": "empty", "nick": "Empty"}}, []

    async def fake_ytdlp(_: str):
        return []

    monkeypatch.setattr(extractors, "resolve_gallery_media", fake_gallery)
    monkeypatch.setattr(extractors, "resolve_ytdlp_entries", fake_ytdlp)

    with pytest.raises(AnalysisError, match="could not read this post"):
        await analyze_url("https://x.com/empty/status/43")


@pytest.mark.asyncio
async def test_analyze_article_classifies_document_media(monkeypatch) -> None:
    post = {
        "author": {"name": "author", "nick": "Article Author"},
        "content": "Article introduction",
        "date": "2026-07-20T10:00:00",
        "article": {
            "id": 987654321,
            "title": "A <Great> Article",
            "date": "2026-07-19T09:00:00",
            "date_updated": "2026-07-21T11:00:00",
            "html": '<p>Body &amp; text</p><script>alert("x")</script>',
        },
    }
    media = [
        ResolvedMedia(
            1,
            "https://pbs.twimg.com/cover.jpg",
            {
                "num": 1,
                "media_id": "cover-100",
                "type": "article:cover",
                "width": 1600,
                "height": 900,
                "description": "Cover <image>",
            },
        ),
        ResolvedMedia(
            2,
            "https://pbs.twimg.com/body.png",
            {
                "num": 2,
                "media_id": 200,
                "type": "article:image",
                "extension": "png",
                "width": 1200,
                "height": 800,
                "alt_text": "Diagram & labels",
            },
        ),
        ResolvedMedia(
            3,
            "https://video.twimg.com/body.mp4",
            {
                "num": 3,
                "media_id": "video-300",
                "type": "article:video",
                "width": 1280,
                "height": 720,
                "duration": 12.5,
            },
        ),
    ]

    async def fake_gallery(_: str):
        return post, media

    async def fake_ytdlp(_: str):
        return [{"formats": [], "height": 720, "width": 1280, "duration": 12.5}]

    async def fake_size(_: str) -> int:
        return 4321

    monkeypatch.setattr(extractors, "resolve_gallery_media", fake_gallery)
    monkeypatch.setattr(extractors, "resolve_ytdlp_entries", fake_ytdlp)
    monkeypatch.setattr(extractors, "_remote_size", fake_size)

    analysis = await analyze_url("https://x.com/author/status/99")

    assert analysis.content_kind.value == "article"
    assert analysis.article is not None
    assert analysis.article.model_dump() == {
        "id": "987654321",
        "title": "A <Great> Article",
        "published_at": "2026-07-19T09:00:00",
        "updated_at": "2026-07-21T11:00:00",
        "html": '<p>Body &amp; text</p><script>alert("x")</script>',
        "html_renderer_version": 1,
    }
    assert [item.role.value for item in analysis.attachments] == [
        "article_cover",
        "article_image",
        "article_video",
    ]
    assert [item.source_id for item in analysis.attachments] == [
        "cover-100",
        "200",
        "video-300",
    ]
    assert [item.media_type.value for item in analysis.attachments] == [
        "photo",
        "photo",
        "video",
    ]
    assert [item.extension for item in analysis.attachments] == ["jpg", "png", "mp4"]
    assert [item.alt_text for item in analysis.attachments] == [
        "Cover <image>",
        "Diagram & labels",
        None,
    ]
    assert analysis.attachments[2].video_ordinal == 1
    assert analysis.attachments[2].qualities[0].id == "q-best"
