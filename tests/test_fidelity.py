from __future__ import annotations

from io import BytesIO
from pathlib import Path

from PIL import Image
from pypdf import PdfReader

from x_media_downloader.article_html import to_html
from x_media_downloader.documents import render_markdown, render_pdf
from x_media_downloader.fidelity import (
    check_markdown,
    compare_blocks,
    evaluate_export,
    scan_html,
)
from x_media_downloader.models import (
    Analysis,
    ArticleMetadata,
    Attachment,
    AttachmentRole,
    MediaType,
    PostMetadata,
)


def _post() -> PostMetadata:
    return PostMetadata(
        post_id="123",
        author_name="Renée Example",
        author_handle="example",
        text="Fallback post text",
        posted_at="2026-07-25T12:00:00+00:00",
    )


def _analysis(html: str, *, attachments: list[Attachment] | None = None) -> Analysis:
    return Analysis(
        id="analysis-1",
        url="https://example.com/status/123",
        post=_post(),
        attachments=attachments or [],
        article=ArticleMetadata(id="article-1", title="Fidelity fixture", html=html),
    )


def _image(path: Path) -> Path:
    Image.new("RGB", (24, 16), color="green").save(path)
    return path


def _full_article_html() -> str:
    return "".join(to_html(_rich_article()))


def _rich_article() -> dict:
    fences = ["```bash\nfirst\n```", "```python\nsecond = 2\n```", "```sh\nthird\n```"]
    return {
        "content_state": {
            "blocks": [
                {"type": "unstyled", "text": "Intro paragraph."},
                {"type": "header-two", "text": "Section One"},
                {"type": "unordered-list-item", "text": "First bullet"},
                {"type": "unordered-list-item", "text": "Second bullet"},
                {"type": "blockquote", "text": "A quoted insight"},
                *(
                    {"type": "atomic", "text": " ", "entityRanges": [{"key": f"md{i}"}]}
                    for i in range(3)
                ),
                {"type": "code-block", "text": "print('native')"},
                {"type": "unstyled", "text": "Closing paragraph."},
            ],
            "entityMap": [
                {
                    "key": f"md{i}",
                    "value": {"type": "MARKDOWN", "data": {"markdown": fence}},
                }
                for i, fence in enumerate(fences)
            ],
        },
        "media_entities": [],
    }


def test_scan_html_clean_render_has_no_issues_and_full_records() -> None:
    html = _full_article_html()
    records, audit = scan_html(html)

    assert audit.is_complete()
    assert not audit.issues
    assert [record.kind for record in records] == [
        "paragraph",
        "heading",
        "list",
        "list",
        "quote",
        "code",
        "code",
        "code",
        "code",
        "paragraph",
    ]


def test_scan_html_reports_unsupported_markers_explicitly() -> None:
    html = (
        "<p>Fine text.</p>"
        '<p data-fidelity="unsupported-block" data-block-type="table">cells | here</p>'
        '<p data-fidelity="unsupported-entity" data-entity-type="TIMELINE">tweet ref</p>'
        '<figure data-fidelity="unresolved-media" data-media-count="2"></figure>'
    )
    records, audit = scan_html(html)

    assert audit.issue_count == 3
    errors = audit.errors()
    warnings = audit.warnings()
    assert len(errors) == 1
    assert len(warnings) == 2
    assert errors[0].source_type == "media"
    assert {issue.entity_type for issue in warnings} == {"table", "TIMELINE"}
    assert all(issue.block_index is not None for issue in audit.issues)
    assert [record.text or record.code for record in records] == [
        "Fine text.",
        "cells | here",
        "tweet ref",
    ]


def test_scan_html_records_lists_quotes_headings_and_media() -> None:
    html = (
        "<h3>Head</h3><blockquote>Quote</blockquote><ul><li>A</li><li>B</li></ul>"
        "<pre>code</pre><hr>"
        '<figure><img data-media-id="media-1"><figcaption>Cap</figcaption></figure>'
    )
    records, audit = scan_html(html)

    assert audit.is_complete()
    assert [
        (record.kind, record.text or record.code, record.level, record.media_id)
        for record in records
    ] == [
        ("heading", "Head", 3, None),
        ("quote", "Quote", 0, None),
        ("list", "A", 0, None),
        ("list", "B", 0, None),
        ("code", "code", 0, None),
        ("divider", "", 0, None),
        ("image", "Cap", 0, "media-1"),
    ]


def test_compare_blocks_detects_dropped_paragraph() -> None:
    html = "<p>First</p><p>Gone</p><p>Last</p>"
    records, audit = scan_html(html)
    kept = [record for record in records if record.plain != "Gone"]
    report = compare_blocks(records, kept)

    assert audit.is_complete()
    assert not report.is_complete()
    errors = report.errors()
    assert len(errors) == 2  # count mismatch + missing paragraph
    assert any(error.content_preview == "Gone" for error in errors)


def test_compare_blocks_accepts_balanced_document() -> None:
    html = "<h2>A</h2><p>B</p><blockquote>C</blockquote><ol><li>D</li></ol><hr>"
    records, audit = scan_html(html)
    assert audit.is_complete()
    assert compare_blocks(records, records).is_complete()


def test_check_markdown_accepts_matching_fences_and_rejects_missing() -> None:
    html = "".join(to_html(_rich_article()))
    records, _audit = scan_html(html)
    markdown = (
        "# Fidelity fixture\n\nBy Author (@handle)\n\n"
        "Intro paragraph.\n\n"
        "## Section One\n\n"
        "- First bullet\n- Second bullet\n\n"
        "> A quoted insight\n\n"
        "```\nfirst\n```\n\n"
        "```\nsecond = 2\n```\n\n"
        "```\nthird\n```\n\n"
        "```\nprint('native')\n```\n\n"
        "Closing paragraph.\n"
    )
    assert check_markdown(records, set(), markdown).is_complete()

    dropped = markdown.replace("```\nsecond = 2\n```\n\n", "")
    report = check_markdown(records, set(), dropped)
    assert not report.is_complete()
    assert any("code" in issue.message for issue in report.errors())


def test_check_markdown_paragraph_heading_quote_and_list_presence() -> None:
    html = (
        "<h2>My Heading</h2>"
        "<p>Some <strong>bold</strong> text with an "
        '<a href="https://example.com/x">inline link</a>.</p>'
        "<blockquote>Think deeply</blockquote>"
        "<ul><li>Alpha</li><li>Beta</li></ul>"
    )
    records, _audit = scan_html(html)
    markdown = (
        "# Title\n\nBy A (@b)\n\n"
        "## My Heading\n\n"
        "Some **bold** text with an [inline link](<https://example.com/x>).\n\n"
        "> Think deeply\n\n"
        "- Alpha\n- Beta\n"
    )
    report = check_markdown(records, set(), markdown)
    assert report.is_complete(), [issue.message for issue in report.issues]

    broken = markdown.replace("- Beta\n", "")
    report = check_markdown(records, set(), broken)
    assert not report.is_complete()


def test_rich_article_passes_full_export_fidelity_round_trip(tmp_path: Path) -> None:
    article = _rich_article()
    html = "".join(to_html(article))
    analysis = Analysis(
        id="analysis-1",
        url="https://example.com/status/123",
        post=_post(),
        attachments=[],
        article=ArticleMetadata(id="article-1", title="Fidelity fixture", html=html),
    )

    markdown = render_markdown(analysis, {})
    pdf = render_pdf(analysis, {})
    report = evaluate_export(analysis, {}, markdown, pdf)

    assert report.is_complete(), [issue.message for issue in report.issues]
    pdf_text = "\n".join(
        page.extract_text() or "" for page in PdfReader(BytesIO(pdf)).pages
    )
    for code in ("first", "second = 2", "third", "print('native')"):
        assert code in pdf_text


def test_media_included_in_document_is_verified(tmp_path: Path) -> None:
    html = (
        "<p>Lead text.</p>"
        '<figure><img data-media-id="src-1" '
        'src="https://pbs.twimg.com/media/photo.jpg">'
        '<figcaption>Figure caption</figcaption></figure>'
    )
    attachment = Attachment(
        id="a-1",
        index=1,
        source_id="src-1",
        media_type=MediaType.PHOTO,
        extension="jpg",
        role=AttachmentRole.ARTICLE_IMAGE,
    )
    analysis = _analysis(html, attachments=[attachment])
    path = _image(tmp_path / "photo.jpg")
    markdown = render_markdown(analysis, {attachment.id: path})
    pdf = render_pdf(analysis, {attachment.id: path})

    report = evaluate_export(analysis, {attachment.id: path}, markdown, pdf)
    assert report.is_complete(), [issue.message for issue in report.issues]
    assert "![photo]" in markdown.decode()
    assert "Figure caption" in "\n".join(
        page.extract_text() or "" for page in PdfReader(BytesIO(pdf)).pages
    )


def test_missing_downloaded_image_is_reported(tmp_path: Path) -> None:
    html = (
        '<figure><img data-media-id="src-1" '
        'src="https://pbs.twimg.com/media/photo.jpg"></figure>'
    )
    attachment = Attachment(
        id="a-1",
        index=1,
        source_id="src-1",
        media_type=MediaType.PHOTO,
        extension="jpg",
        role=AttachmentRole.ARTICLE_IMAGE,
    )
    analysis = _analysis(html, attachments=[attachment])
    path = _image(tmp_path / "photo.jpg")
    markdown = render_markdown(analysis, {attachment.id: path})
    pdf_without_image = render_pdf(analysis, {})

    report = evaluate_export(analysis, {attachment.id: path}, markdown, pdf_without_image)

    assert not report.is_complete()
    assert any("image" in issue.message.lower() for issue in report.errors())


def test_unknown_entity_article_exports_with_warning(tmp_path: Path) -> None:
    article = {
        "content_state": {
            "blocks": [
                {"type": "unstyled", "text": "Plain."},
                {"type": "atomic", "text": " ", "entityRanges": [{"key": "x"}]},
            ],
            "entityMap": [
                {"key": "x", "value": {"type": "TWEET", "data": {"text": "embedded tweet"}}}
            ],
        },
        "media_entities": [],
    }
    html = "".join(to_html(article))
    analysis = _analysis(html)
    markdown = render_markdown(analysis, {})

    report = evaluate_export(analysis, {}, markdown, None)

    assert report.is_complete()
    warnings = report.warnings()
    assert any("TWEET" in issue.message for issue in warnings)
    assert any(issue.severity == "warning" for issue in report.issues)


def test_plain_post_without_article_is_not_audited(tmp_path: Path) -> None:
    analysis = Analysis(
        id="analysis-1",
        url="https://example.com/status/123",
        post=_post(),
        attachments=[],
    )
    markdown = render_markdown(analysis, {})
    assert evaluate_export(analysis, {}, markdown, None).is_complete()