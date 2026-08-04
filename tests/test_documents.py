from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from PIL import Image
from pypdf import PdfReader

import x_media_downloader.documents as documents
from x_media_downloader.documents import (
    DocumentError,
    DocumentMedia,
    document_base,
    markdown_assets_name,
    render_markdown,
    render_markdown_text,
    render_pdf,
    write_document,
    write_pdf,
)
from x_media_downloader.models import (
    Analysis,
    ArticleMetadata,
    Attachment,
    AttachmentRole,
    MediaType,
    OutputFormat,
    PostMetadata,
)
from x_media_downloader.verification import VerificationError, verify_markdown, verify_pdf


def _post() -> PostMetadata:
    return PostMetadata(
        post_id="123",
        author_name="Renée Example",
        author_handle="example",
        text="Fallback post text",
        posted_at="2026-07-25T12:00:00+00:00",
    )


def _attachment(
    identifier: str,
    role: AttachmentRole,
    *,
    alt_text: str,
    source_id: str | None = None,
    media_type: MediaType = MediaType.PHOTO,
) -> Attachment:
    return Attachment(
        id=identifier,
        index=1,
        media_type=media_type,
        extension="png" if media_type == MediaType.PHOTO else "mp4",
        role=role,
        source_id=source_id,
        alt_text=alt_text,
    )


def _image(path: Path, color: str) -> None:
    Image.new("RGB", (24, 16), color=color).save(path)


def test_markdown_sanitizes_html_links_and_maps_known_body_images(tmp_path: Path) -> None:
    cover = tmp_path / "cover image.png"
    body = tmp_path / "body image.png"
    _image(cover, "navy")
    _image(body, "green")
    media = [
        DocumentMedia(_attachment("cover", AttachmentRole.ARTICLE_COVER, alt_text="Cover"), cover),
        DocumentMedia(
            _attachment("body", AttachmentRole.ARTICLE_IMAGE, alt_text="Body diagram"), body
        ),
    ]
    article = ArticleMetadata(
        id="article-1",
        title="A Unicode title — café",
        published_at="2026-07-24T10:30:00+00:00",
        html="""
            <h2>Safe <em>structure</em></h2>
            <p>Hello <strong>world</strong>. <a href="https://example.com/a?q=1">Read more</a>.</p>
            <p><a href="javascript:alert(1)">Unsafe label</a><script>alert(2)</script></p>
            <img src="https://remote.invalid/untrusted.png" alt="remote">
            <iframe src="https://remote.invalid/"></iframe>
        """,
    )

    first = render_markdown_text(
        _post(), media, article=article, source_url="https://x.com/example/status/123"
    )
    second = render_markdown_text(
        _post(), media, article=article, source_url="https://x.com/example/status/123"
    )

    assert first == second
    assert first.encode("utf-8").decode("utf-8") == first
    assert "## Safe *structure*" in first
    assert "Hello **world**." in first
    assert "[Read more](<https://example.com/a?q=1>)" in first
    assert "Unsafe label" in first
    assert "javascript:" not in first
    assert "alert(2)" not in first
    assert "iframe" not in first
    assert "remote.invalid/untrusted" not in first
    assert "![Body diagram](<body%20image.png>)" in first
    assert first.index("Cover") < first.index("Body diagram")
    assert "## Attachments" not in first

    pdf_path = tmp_path / "article.pdf"
    write_pdf(pdf_path, _post(), media, article=article)
    pdf_text = "\n".join(page.extract_text() or "" for page in PdfReader(pdf_path).pages)
    assert pdf_text.index("Cover") < pdf_text.index("Safe structure")
    assert pdf_text.index("Safe structure") < pdf_text.index("Body diagram")


def test_markdown_normalizes_nested_inline_whitespace_without_splitting_words() -> None:
    article = ArticleMetadata(
        id="article-1",
        title="Inline fidelity",
        html=(
            "<p><span>We</span><span>'re</span> building "
            "<strong> clean <em>nested</em> words </strong> and don<span>'t</span> split."
            "<br>Next line.</p>"
        ),
    )

    markdown = render_markdown_text(_post(), [], article=article)

    assert "We're building **clean *nested* words** and don't split.  \nNext line." in markdown
    assert "<span>" not in markdown
    assert "<strong>" not in markdown
    assert "<em>" not in markdown


def test_canonical_media_ids_do_not_shift_when_an_image_is_deselected(tmp_path: Path) -> None:
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    selected_path = media_dir / "selected image.png"
    _image(selected_path, "green")
    deselected = _attachment(
        "first", AttachmentRole.ARTICLE_IMAGE, alt_text="Deselected", source_id="media-first"
    )
    selected = _attachment(
        "second", AttachmentRole.ARTICLE_IMAGE, alt_text="Selected", source_id="media-second"
    )
    article = ArticleMetadata(
        id="article-1",
        title="Mapped media",
        html_renderer_version=1,
        html=(
            '<img data-media-id="media-first"><p>Between images</p>'
            '<img data-media-id="media-second">'
        ),
    )
    analysis = Analysis(
        id="analysis-1",
        url="https://x.com/example/status/123",
        post=_post(),
        attachments=[deselected, selected],
        article=article,
    )

    markdown = render_markdown(analysis, {selected.id: selected_path}).decode()

    assert "Deselected" not in markdown
    assert markdown.index("Between images") < markdown.index("![Selected]")
    assert "![Selected](<media/selected%20image.png>)" in markdown
    assert "## Attachments" not in markdown


def test_article_video_media_id_becomes_relative_document_links(tmp_path: Path) -> None:
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    video_path = media_dir / "clip.mp4"
    video_path.write_bytes(b"video")
    video = _attachment(
        "video",
        AttachmentRole.ARTICLE_VIDEO,
        alt_text="Article clip",
        source_id="media-video",
        media_type=MediaType.VIDEO,
    )
    article = ArticleMetadata(
        id="article-1",
        title="Video mapping",
        html_renderer_version=1,
        html=(
            '<figure><video data-media-id="media-video"></video>'
            "<figcaption>Clip caption</figcaption></figure>"
        ),
    )
    analysis = Analysis(
        id="analysis-1",
        url="https://x.com/example/status/123",
        post=_post(),
        attachments=[video],
        article=article,
    )

    markdown = render_markdown(analysis, {video.id: video_path}).decode()
    pdf_path = tmp_path / "video.pdf"
    pdf_path.write_bytes(render_pdf(analysis, {video.id: video_path}))
    reader = PdfReader(pdf_path)
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    links = [
        annotation.get_object().get("/A", {}).get("/URI")
        for page in reader.pages
        for annotation in page.get("/Annots", [])
    ]

    assert "[Article clip](<media/clip.mp4>)" in markdown
    assert "Clip caption" in markdown
    assert "Video: Article clip" in text
    assert "media/clip.mp4" in links


def test_pdf_ordered_lists_metadata_and_publication_date_are_deterministic(
    tmp_path: Path,
) -> None:
    article = ArticleMetadata(
        id="article-1",
        title="List metadata",
        published_at="2026-07-24T10:30:00+00:00",
        html=(
            "<ul><li>Bullet</li></ul>"
            "<ol start='3'><li>Third</li><li>Fourth</li></ol>"
        ),
    )
    source_url = "https://x.com/example/status/123"
    first = tmp_path / "first.pdf"
    second = tmp_path / "second.pdf"

    write_pdf(first, _post(), [], article=article, source_url=source_url)
    write_pdf(second, _post(), [], article=article, source_url=source_url)

    assert first.read_bytes() == second.read_bytes()
    reader = PdfReader(first)
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    lines = text.splitlines()
    assert "Bullet" in lines
    third = lines.index("Third")
    fourth = lines.index("Fourth")
    assert lines[third - 1] == "3"
    assert lines[fourth - 1] == "4"
    assert third < fourth
    metadata = reader.metadata
    assert metadata.title == "List metadata"
    assert metadata.author == "Renée Example (@example)"
    assert metadata.subject == f"Source: {source_url}"
    assert metadata.creator == "crXte"
    assert metadata.keywords == "X, crXte, article, @example"
    assert metadata.creation_date == datetime.fromisoformat("2026-07-24T10:30:00+00:00")
    assert metadata.modification_date == metadata.creation_date


def test_write_pdf_creates_deterministic_native_readable_pdf(tmp_path: Path) -> None:
    article = ArticleMetadata(
        id="article-1",
        title="Native PDF café Привет",
        html="<h2>Section</h2><p>Text with <strong>formatting</strong> and 😀.</p>",
    )
    first = tmp_path / "first.pdf"
    second = tmp_path / "second.pdf"

    assert write_pdf(first, _post(), [], article=article) == first.stat().st_size
    write_pdf(second, _post(), [], article=article)

    assert first.read_bytes() == second.read_bytes()
    assert first.read_bytes().startswith(b"%PDF-")
    assert verify_pdf(first) == first.stat().st_size
    text = "\n".join(page.extract_text() or "" for page in PdfReader(first).pages)
    assert "Native PDF café Привет" in text
    assert "Section" in text
    assert "formatting" in text


def test_queue_rendering_surface_returns_bytes_and_relative_media_paths(tmp_path: Path) -> None:
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    body = media_dir / "body image.png"
    _image(body, "green")
    attachment = _attachment("body", AttachmentRole.ARTICLE_IMAGE, alt_text="Body")
    article = ArticleMetadata(id="article-1", title="Article", html="<p>Body</p><img>")
    analysis = Analysis(
        id="analysis-1",
        url="https://x.com/example/status/123",
        post=_post(),
        attachments=[attachment],
        article=article,
    )

    markdown = render_markdown(analysis, {attachment.id: body})
    pdf = render_pdf(analysis, {attachment.id: body})

    assert document_base(analysis) == "@example_123"
    assert markdown_assets_name(analysis) == "@example_123_assets"
    assert b"media/body%20image.png" in markdown
    assert pdf.startswith(b"%PDF-")


def test_queue_renderers_wrap_render_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    analysis = Analysis(
        id="analysis-1",
        url="https://x.com/example/status/123",
        post=_post(),
        attachments=[],
    )

    def fail(*args: object, **kwargs: object) -> None:
        raise ValueError("broken input")

    monkeypatch.setattr(documents, "render_markdown_text", fail)
    with pytest.raises(DocumentError, match="Markdown") as markdown_error:
        render_markdown(analysis, {})
    assert isinstance(markdown_error.value.__cause__, ValueError)

    monkeypatch.setattr(documents, "_build_pdf", fail)
    with pytest.raises(DocumentError, match="PDF") as pdf_error:
        render_pdf(analysis, {})
    assert isinstance(pdf_error.value.__cause__, ValueError)


def test_write_document_dispatches_markdown_and_verifies_utf8(tmp_path: Path) -> None:
    path = tmp_path / "post.md"
    size = write_document(
        path,
        OutputFormat.MARKDOWN,
        _post(),
        [],
        source_url="https://x.com/example/status/123",
    )

    assert size == path.stat().st_size
    assert verify_markdown(path) == size
    assert "Fallback post text" in path.read_text(encoding="utf-8")


def test_document_verification_rejects_unsafe_or_unreadable_files(tmp_path: Path) -> None:
    unsafe = tmp_path / "unsafe.md"
    unsafe.write_text("[click](javascript:alert(1))\n", encoding="utf-8")
    with pytest.raises(VerificationError, match="unsafe link"):
        verify_markdown(unsafe)

    invalid_utf8 = tmp_path / "invalid.md"
    invalid_utf8.write_bytes(b"\xff\xfe")
    with pytest.raises(VerificationError, match="UTF-8"):
        verify_markdown(invalid_utf8)

    invalid_pdf = tmp_path / "invalid.pdf"
    invalid_pdf.write_bytes(b"not a pdf")
    with pytest.raises(VerificationError, match="could not be read"):
        verify_pdf(invalid_pdf)
