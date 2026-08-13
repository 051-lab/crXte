"""Raw X content_state boundary tests.

These prove the fidelity system accounts for meaningful constructs in the
original article source before ``article_html.to_html()`` renders them, and
that the required invariant holds:

    Every meaningful raw source construct must either map to a
    rendered/canonical representation or produce an explicit fidelity issue.

The suite intentionally exercises the raw -> rendered HTML boundary with
synthetic payloads so normal CI never depends on X network access.
"""

from __future__ import annotations

import pytest

from x_media_downloader.article_html import to_html
from x_media_downloader.fidelity import (
    evaluate_export,
    scan_content_state,
)
from x_media_downloader.models import Analysis, ArticleMetadata, PostMetadata


def _post() -> PostMetadata:
    return PostMetadata(
        post_id="123",
        author_name="Renée Example",
        author_handle="example",
        text="Fallback post text",
        posted_at="2026-07-25T12:00:00+00:00",
    )


def _article(
    html: str,
    content_state=None,
    media_entities=None,
) -> ArticleMetadata:
    return ArticleMetadata(
        id="article-1",
        title="Fidelity fixture",
        html=html,
        content_state=content_state,
        media_entities=media_entities,
        html_renderer_version=1,
    )


def _analysis(html: str, *, content_state=None, media_entities=None) -> Analysis:
    return Analysis(
        id="analysis-1",
        url="https://example.com/status/123",
        post=_post(),
        attachments=[],
        article=_article(html, content_state, media_entities),
    )


def _block(type_: str, text: str = "", **extra) -> dict:
    block = {"type": type_, "text": text}
    block.update(extra)
    return block


def _atomic(key: str, text: str = " ") -> dict:
    return _block("atomic", text, entityRanges=[{"key": key}])


def _render(*blocks, entity_map=None, media_entities=None) -> tuple[dict, str]:
    article = {
        "content_state": {
            "blocks": list(blocks),
            "entityMap": entity_map or {},
        },
        "media_entities": media_entities or [],
    }
    return article, "".join(to_html(article))


# ---------------------------------------------------------------------------
# scan_content_state: malformed whole-article structures
# ---------------------------------------------------------------------------


def test_scan_content_state_rejects_missing_content_state() -> None:
    records, report = scan_content_state(None)
    assert records == ()
    assert not report.is_complete()
    assert report.issues[0].stage == "source"
    assert "content state" in report.issues[0].message


def test_scan_content_state_rejects_non_dict_payload() -> None:
    records, report = scan_content_state("corrupted")
    assert records == ()
    assert not report.is_complete()


def test_scan_content_state_rejects_missing_blocks() -> None:
    records, report = scan_content_state({"entityMap": []})
    assert records == ()
    assert not report.is_complete()
    assert "blocks" in report.issues[0].message


# ---------------------------------------------------------------------------
# Whole-article silent drop: valid raw source, empty render
# ---------------------------------------------------------------------------


def test_whole_article_silent_drop_is_an_error() -> None:
    content_state = {
        "blocks": [
            _block("unstyled", "Alpha"),
            _block("unstyled", "Omega"),
        ],
        "entityMap": [],
    }
    report = evaluate_export(_analysis("", content_state=content_state), None, None, None)
    assert not report.is_complete()
    assert any(
        issue.stage == "source→html" and "2 paragraph(s)" in issue.message
        for issue in report.issues
    )


# ---------------------------------------------------------------------------
# Unknown raw block types
# ---------------------------------------------------------------------------


def test_unknown_raw_block_dropped_by_renderer_is_an_error() -> None:
    content_state = {
        "blocks": [_block("hologram-card", "Meaningful text")],
        "entityMap": [],
    }
    report = evaluate_export(_analysis("", content_state=content_state), None, None, None)
    assert not report.is_complete()
    assert any(
        issue.stage == "source→html" and "hologram" in issue.message for issue in report.issues
    )


def test_unknown_raw_block_preserved_via_marker_is_warning() -> None:
    content_state = {
        "blocks": [_block("hologram-card", "Meaningful text")],
        "entityMap": [],
    }
    html = (
        '<p data-fidelity="unsupported-block" data-block-type="hologram-card">Meaningful text</p>'
    )
    report = evaluate_export(_analysis(html, content_state=content_state), None, None, None)
    assert report.is_complete()
    assert any(issue.severity == "warning" for issue in report.issues)


# ---------------------------------------------------------------------------
# Unknown atomic entity types
# ---------------------------------------------------------------------------


def test_unknown_atomic_entity_dropped_without_marker_is_an_error() -> None:
    content_state = {
        "blocks": [
            _atomic("w1"),
            _block("unstyled", "Lead paragraph."),
        ],
        "entityMap": [
            {"key": "w1", "value": {"type": "CUSTOM_WIDGET", "data": {"title": "Widget"}}}
        ],
    }
    report = evaluate_export(_analysis("", content_state=content_state), None, None, None)
    assert not report.is_complete()
    assert any(issue.stage == "source→html" for issue in report.issues)


def test_unknown_atomic_entity_preserved_via_marker_is_warning() -> None:
    content_state = {
        "blocks": [
            _atomic("w1"),
            _block("unstyled", "Lead paragraph."),
        ],
        "entityMap": [
            {"key": "w1", "value": {"type": "CUSTOM_WIDGET", "data": {"title": "Widget"}}}
        ],
    }
    html = (
        '<p data-fidelity="unsupported-entity" data-entity-type="CUSTOM_WIDGET">'
        "Widget</p><p>Lead paragraph.</p>"
    )
    report = evaluate_export(_analysis(html, content_state=content_state), None, None, None)
    assert report.is_complete()
    assert any(issue.severity == "warning" for issue in report.issues)


# ---------------------------------------------------------------------------
# Missing entity relationships
# ---------------------------------------------------------------------------


def test_missing_entity_reference_is_detected_from_raw_source() -> None:
    content_state = {
        "blocks": [_block("atomic", " ", entityRanges=[{"key": "ghost"}])],
        "entityMap": [],
    }
    report = evaluate_export(_analysis("", content_state=content_state), None, None, None)
    assert not report.is_complete()
    assert any("entity" in issue.message.lower() for issue in report.issues)


# ---------------------------------------------------------------------------
# MEDIA entities
# ---------------------------------------------------------------------------


def test_media_entity_malformed_media_items_is_an_error() -> None:
    content_state = {
        "blocks": [_atomic("m1")],
        "entityMap": [
            {
                "key": "m1",
                "value": {
                    "type": "MEDIA",
                    "data": {"caption": "c", "mediaItems": "oops"},
                },
            }
        ],
    }
    report = evaluate_export(_analysis("", content_state=content_state), None, None, None)
    assert not report.is_complete()
    assert any("mediaItems" in issue.message for issue in report.issues)


def test_media_item_without_media_id_is_an_error() -> None:
    content_state = {
        "blocks": [_atomic("m1")],
        "entityMap": [
            {
                "key": "m1",
                "value": {"type": "MEDIA", "data": {"mediaItems": [{"caption": "x"}]}},
            }
        ],
    }
    report = evaluate_export(_analysis("", content_state=content_state), None, None, None)
    assert not report.is_complete()
    assert any("media" in issue.message.lower() for issue in report.issues)


def test_media_item_not_resolved_in_render_is_detected() -> None:
    content_state = {
        "blocks": [_atomic("m1")],
        "entityMap": [
            {
                "key": "m1",
                "value": {
                    "type": "MEDIA",
                    "data": {"mediaItems": [{"mediaId": "photo-1"}]},
                },
            }
        ],
    }
    report = evaluate_export(
        _analysis("", content_state=content_state, media_entities={}),
        None,
        None,
        None,
    )
    assert not report.is_complete()
    assert any(issue.stage == "source→html" for issue in report.issues)


# ---------------------------------------------------------------------------
# Known construct deliberately absent from rendered HTML
# ---------------------------------------------------------------------------


def test_known_construct_intentionally_absent_from_render_is_detected() -> None:
    content_state = {
        "blocks": [
            _block("unstyled", "Alpha"),
            _block("unstyled", "Omega"),
        ],
        "entityMap": [],
    }
    html = "<p>Alpha</p>"
    report = evaluate_export(_analysis(html, content_state=content_state), None, None, None)
    assert not report.is_complete()
    assert any(
        issue.stage == "source→html" and "Omega" in (issue.content_preview or "")
        for issue in report.issues
    )


# ---------------------------------------------------------------------------
# Multiplicity and ordering
# ---------------------------------------------------------------------------


def test_duplicate_substitution_is_detected() -> None:
    content_state = {
        "blocks": [
            _block("unstyled", "A"),
            _block("unstyled", "A"),
            _block("unstyled", "B"),
        ],
        "entityMap": [],
    }
    html = "<p>A</p><p>B</p><p>B</p>"
    report = evaluate_export(_analysis(html, content_state=content_state), None, None, None)
    assert not report.is_complete()
    assert any(issue.stage == "source→html" for issue in report.issues)


def test_reordered_code_blocks_are_detected() -> None:
    content_state = {
        "blocks": [_atomic(f"md{i}") for i in range(3)],
        "entityMap": [
            {
                "key": f"md{i}",
                "value": {
                    "type": "MARKDOWN",
                    "data": {
                        "markdown": fence,
                    },
                },
            }
            for i, fence in enumerate(["```\ncode-A\n```", "```\ncode-B\n```", "```\ncode-C\n```"])
        ],
    }
    html = (
        "<pre><code>code-B</code></pre><pre><code>code-A</code></pre><pre><code>code-C</code></pre>"
    )
    report = evaluate_export(_analysis(html, content_state=content_state), None, None, None)
    assert not report.is_complete()
    assert any(issue.stage == "source→html" and "code" in issue.message for issue in report.issues)


def test_identical_duplicate_paragraphs_do_not_false_positive() -> None:
    content_state = {
        "blocks": [
            _block("unstyled", "A"),
            _block("unstyled", "B"),
            _block("unstyled", "A"),
        ],
        "entityMap": [],
    }
    html = "<p>A</p><p>B</p><p>A</p>"
    report = evaluate_export(_analysis(html, content_state=content_state), None, None, None)
    assert report.is_complete(), [issue.message for issue in report.issues]


# ---------------------------------------------------------------------------
# Atomic MARKDOWN regression protection (13 fences, canonical article shape)
# ---------------------------------------------------------------------------


def _thirteen_fences() -> tuple[dict, str]:
    fences = [f"```\nsection-{index}\n```" for index in range(1, 14)]
    blocks = [_atomic(f"md{index}") for index in range(1, 14)]
    entity_map = [
        {
            "key": f"md{index}",
            "value": {"type": "MARKDOWN", "data": {"markdown": fence}},
        }
        for index, fence in enumerate(fences, start=1)
    ]
    content_state = {"blocks": blocks, "entityMap": entity_map}
    return content_state, "".join(
        to_html({"content_state": content_state, "media_entities": []})
    )


@pytest.mark.parametrize("renderer", [False, True])
def test_thirteen_markdown_fences_round_trip_stays_clean(renderer: bool) -> None:
    content_state, html = _thirteen_fences()
    from x_media_downloader.documents import render_markdown

    md = render_markdown(_analysis(html, content_state=content_state), {})
    report = evaluate_export(
        _analysis(html, content_state=content_state),
        {} if renderer else None,
        md if renderer else None,
        None,
    )
    assert report.is_complete(), [issue.message for issue in report.issues]


def test_thirteen_markdown_fences_reordered_are_detected() -> None:
    content_state, _html = _thirteen_fences()
    html = (
        "<pre><code>section-2</code></pre>"
        "<pre><code>section-1</code></pre>"
        + "".join(
            f"<pre><code>section-{index}</code></pre>" for index in range(3, 14)
        )
    )
    report = evaluate_export(_analysis(html, content_state=content_state), None, None, None)
    assert any(
        issue.stage == "source→html"
        and "position" in issue.message
        and issue.content_preview == "section-1"
        for issue in report.issues
    )


# ---------------------------------------------------------------------------
# Media exclusion must stay error-free at the new boundary
# ---------------------------------------------------------------------------


def test_excluded_media_produce_no_fidelity_errors() -> None:
    content_state = {
        "blocks": [
            _block("unstyled", "Lead paragraph."),
            _atomic("m1"),
        ],
        "entityMap": [
            {
                "key": "m1",
                "value": {
                    "type": "MEDIA",
                    "data": {"mediaItems": [{"mediaId": "photo-1"}]},
                },
            }
        ],
    }
    html = (
        "<p>Lead paragraph.</p>"
        '<figure><img data-media-id="photo-1" src="https://example.com/pic.jpg"></figure>'
    )
    report = evaluate_export(
        _analysis(
            html,
            content_state=content_state,
            media_entities={"photo-1": {"media_id": "photo-1"}},
        ),
        {},
        None,
        None,
    )
    assert report.is_complete(), [issue.message for issue in report.issues]
    assert all(issue.source_type != "media" for issue in report.issues)


# ---------------------------------------------------------------------------
# Security: X-controlled text stays escaped and plain
# ---------------------------------------------------------------------------


def test_x_controlled_preview_text_is_escaped_and_plain() -> None:
    payload = "<img src=x onerror=alert(1)>"
    content_state = {
        "blocks": [_atomic("w1")],
        "entityMap": [
            {
                "key": "w1",
                "value": {"type": "CUSTOM_WIDGET", "data": {"title": payload}},
            }
        ],
    }
    html_fragments = to_html(
        {"content_state": content_state, "media_entities": []}
    )
    html = "".join(html_fragments)
    assert "<img src=x" not in html
    assert "&lt;img" in html

    report = evaluate_export(_analysis(html, content_state=content_state), None, None, None)
    previews = [
        issue.content_preview for issue in report.issues if issue.content_preview
    ]
    assert previews
    assert all(preview == payload for preview in previews)


# ---------------------------------------------------------------------------
# Integer entity keys (graph payloads emit key 0 as an int, not "0")
# ---------------------------------------------------------------------------


def test_integer_entity_key_zero_is_resolved() -> None:
    content_state = {
        "blocks": [
            _block("atomic", " ", entityRanges=[{"key": 0}]),
            _block("unstyled", "Lead paragraph."),
        ],
        "entityMap": [{"key": "0", "value": {"type": "TWEET", "data": {}}}],
    }
    html = (
        '<p data-fidelity="unsupported-entity" data-entity-type="TWEET"> </p>'
        "<p>Lead paragraph.</p>"
    )
    report = evaluate_export(_analysis(html, content_state=content_state), None, None, None)
    assert not report.errors(), [issue.message for issue in report.errors()]
    assert report.is_complete(), [issue.message for issue in report.issues]


def test_required_markdown_targets_use_the_export_reference(tmp_path) -> None:
    from x_media_downloader.documents import render_markdown
    from x_media_downloader.models import (
        Analysis,
        ArticleMetadata,
        Attachment,
        AttachmentRole,
        ContentKind,
        MediaType,
    )

    media_dir = tmp_path / "root" / "media"
    media_dir.mkdir(parents=True)
    image_path = media_dir / "01.jpg"
    image_path.write_bytes(b"junk")

    attachment = Attachment(
        id="a-1",
        index=1,
        source_id="photo-1",
        media_type=MediaType.PHOTO,
        extension="jpg",
        role=AttachmentRole.ARTICLE_IMAGE,
    )
    content_state = {
        "blocks": [_atomic("m1")],
        "entityMap": [
            {
                "key": "m1",
                "value": {
                    "type": "MEDIA",
                    "data": {"mediaItems": [{"mediaId": "photo-1"}]},
                },
            }
        ],
    }
    media_payload = [
        {
            "media_id": "photo-1",
            "media_info": {
                "original_img_url": "https://example.com/pic.jpg",
                "original_img_width": 100,
                "original_img_height": 100,
            },
        }
    ]
    article = {"content_state": content_state, "media_entities": media_payload}
    html = "".join(to_html(article))
    assert "data-media-id=" in html
    analysis = Analysis(
        id="analysis-ref",
        url="https://example.com/status/1",
        post=_post(),
        attachments=[attachment],
        content_kind=ContentKind.ARTICLE,
        article=ArticleMetadata(
            id="article-1",
            title="Ref",
            html=html,
            html_renderer_version=1,
            content_state=content_state,
            media_entities=media_payload,
        ),
    )
    md = render_markdown(analysis, {attachment.id: image_path})
    report = evaluate_export(analysis, {attachment.id: image_path}, md, None)
    assert report.is_complete(), [issue.message for issue in report.issues]


# ---------------------------------------------------------------------------
# List/quote multiplicity at the raw boundary
# ---------------------------------------------------------------------------


def test_duplicate_list_items_substituted_in_render_are_detected() -> None:
    content_state = {
        "blocks": [
            _block("unordered-list-item", "A"),
            _block("unordered-list-item", "A"),
            _block("unordered-list-item", "B"),
        ],
        "entityMap": [],
    }
    html = "<ul><li>A</li><li>B</li><li>B</li></ul>"
    report = evaluate_export(_analysis(html, content_state=content_state), None, None, None)
    assert not report.is_complete()
    assert any(
        issue.stage == "source→html" and "list item" in issue.message
        for issue in report.issues
    )


def test_duplicate_quotes_substituted_in_render_are_detected() -> None:
    content_state = {
        "blocks": [
            _block("blockquote", "A"),
            _block("blockquote", "A"),
            _block("blockquote", "B"),
        ],
        "entityMap": [],
    }
    html = "<blockquote>A</blockquote><blockquote>B</blockquote><blockquote>B</blockquote>"
    report = evaluate_export(_analysis(html, content_state=content_state), None, None, None)
    assert not report.is_complete()
    assert any(
        issue.stage == "source→html" and "quote" in issue.message
        for issue in report.issues
    )


def test_legitimate_duplicate_quotes_and_list_items_stay_clean() -> None:
    content_state = {
        "blocks": [
            _block("blockquote", "Q"),
            _block("blockquote", "Q"),
            _block("unordered-list-item", "A"),
            _block("unordered-list-item", "A"),
            _block("unordered-list-item", "B"),
        ],
        "entityMap": [],
    }
    html = (
        "<blockquote>Q</blockquote><blockquote>Q</blockquote>"
        "<ul><li>A</li><li>A</li><li>B</li></ul>"
    )
    report = evaluate_export(_analysis(html, content_state=content_state), None, None, None)
    assert report.is_complete(), [issue.message for issue in report.issues]


# ---------------------------------------------------------------------------
# Heading levels at the raw boundary
# ---------------------------------------------------------------------------


def test_heading_level_mutation_in_render_is_detected() -> None:
    content_state = {
        "blocks": [_block("header-two", "Section")],
        "entityMap": [],
    }
    html = "<h1>Section</h1>"
    report = evaluate_export(_analysis(html, content_state=content_state), None, None, None)
    assert not report.is_complete()
    assert any(
        issue.stage == "source→html" and "heading" in issue.message.lower()
        for issue in report.issues
    )


def test_heading_levels_round_trip_stays_clean() -> None:
    content_state = {
        "blocks": [
            _block("header-three", "Tertiary"),
            _block("header-six", "Deep"),
        ],
        "entityMap": [],
    }
    html = "<h3>Tertiary</h3><h6>Deep</h6>"
    report = evaluate_export(_analysis(html, content_state=content_state), None, None, None)
    assert report.is_complete(), [issue.message for issue in report.issues]


# ---------------------------------------------------------------------------
# Per-item media resolution and rendered identity
# ---------------------------------------------------------------------------


def _media_source(entity_key: str, media_ids: list[str]) -> dict:
    return {
        "blocks": [_atomic(entity_key)],
        "entityMap": [
            {
                "key": entity_key,
                "value": {
                    "type": "MEDIA",
                    "data": {"mediaItems": [{"mediaId": mid} for mid in media_ids]},
                },
            }
        ],
    }


def test_mixed_media_resolution_identifies_the_unresolved_item() -> None:
    content_state = _media_source("m1", ["photo-a", "photo-b"])
    html = '<figure><img data-media-id="photo-a" src="https://example.com/a.jpg"></figure>'
    report = evaluate_export(
        _analysis(
            html,
            content_state=content_state,
            media_entities={"photo-a": {"media_id": "photo-a"}},
        ),
        {},
        None,
        None,
    )
    assert not report.is_complete()
    assert any(
        issue.stage == "source→html"
        and issue.source_type == "media"
        and "photo-b" in issue.message
        for issue in report.issues
    )


def test_media_identity_substitution_is_detected() -> None:
    content_state = _media_source("m1", ["photo-a", "photo-b"])
    html = (
        '<figure><img data-media-id="photo-a" src="https://example.com/a.jpg"></figure>'
        '<figure><img data-media-id="photo-a" src="https://example.com/a.jpg"></figure>'
    )
    report = evaluate_export(
        _analysis(
            html,
            content_state=content_state,
            media_entities={
                "photo-a": {"media_id": "photo-a"},
                "photo-b": {"media_id": "photo-b"},
            },
        ),
        {},
        None,
        None,
    )
    assert not report.is_complete()
    assert any(
        issue.stage == "source→html"
        and issue.source_type == "media"
        and "photo-b" in issue.message
        for issue in report.issues
    )


def test_media_identity_matches_in_clean_render() -> None:
    content_state = _media_source("m1", ["photo-a", "photo-b"])
    html = (
        '<figure><img data-media-id="photo-a" src="https://example.com/a.jpg">'
        "<figcaption>First</figcaption></figure>"
        '<figure><img data-media-id="photo-b" src="https://example.com/b.jpg">'
        "<figcaption>Second</figcaption></figure>"
    )
    report = evaluate_export(
        _analysis(
            html,
            content_state=content_state,
            media_entities={
                "photo-a": {"media_id": "photo-a"},
                "photo-b": {"media_id": "photo-b"},
            },
        ),
        {},
        None,
        None,
    )
    assert report.is_complete(), [issue.message for issue in report.issues]


def test_unresolved_media_flagged_by_renderer_is_not_double_reported() -> None:
    content_state = _media_source("m1", ["photo-1"])
    html = '<figure data-fidelity="unresolved-media" data-media-count="1"></figure>'
    report = evaluate_export(
        _analysis(html, content_state=content_state, media_entities={}),
        None,
        None,
        None,
    )
    errors = report.errors()
    assert any(
        issue.stage == "article_html" and issue.source_type == "media"
        for issue in errors
    )
    assert not any(
        issue.stage == "source→html" and issue.source_type == "media"
        for issue in errors
    )


def test_unresolved_marker_in_one_entity_does_not_mask_another_entity() -> None:
    content_state = {
        "blocks": [
            _atomic("m1"),
            _atomic("m2"),
        ],
        "entityMap": [
            {
                "key": "m1",
                "value": {
                    "type": "MEDIA",
                    "data": {"mediaItems": [{"mediaId": "photo-a"}]},
                },
            },
            {
                "key": "m2",
                "value": {
                    "type": "MEDIA",
                    "data": {
                        "mediaItems": [{"mediaId": "photo-b"}, {"mediaId": "photo-c"}]
                    },
                },
            },
        ],
    }
    html = (
        '<figure data-fidelity="unresolved-media" data-media-count="1"></figure>'
        '<figure><img data-media-id="photo-b" src="https://example.com/b.jpg"></figure>'
    )
    report = evaluate_export(
        _analysis(
            html,
            content_state=content_state,
            media_entities={"photo-b": {"media_id": "photo-b"}},
        ),
        None,
        None,
        None,
    )
    errors = report.errors()
    assert any(
        issue.stage == "article_html" and issue.source_type == "media"
        for issue in errors
    )
    assert any(
        issue.stage == "source→html"
        and issue.source_type == "media"
        and "photo-c" in issue.message
        for issue in errors
    )


def test_multiple_fully_unresolved_entities_are_each_covered_by_their_marker() -> None:
    content_state = {
        "blocks": [
            _atomic("m1"),
            _atomic("m2"),
        ],
        "entityMap": [
            {
                "key": "m1",
                "value": {
                    "type": "MEDIA",
                    "data": {"mediaItems": [{"mediaId": "photo-a"}]},
                },
            },
            {
                "key": "m2",
                "value": {
                    "type": "MEDIA",
                    "data": {
                        "mediaItems": [{"mediaId": "photo-b"}, {"mediaId": "photo-c"}]
                    },
                },
            },
        ],
    }
    html = (
        '<figure data-fidelity="unresolved-media" data-media-count="1"></figure>'
        '<figure data-fidelity="unresolved-media" data-media-count="2"></figure>'
    )
    report = evaluate_export(
        _analysis(html, content_state=content_state, media_entities={}),
        None,
        None,
        None,
    )
    errors = report.errors()
    assert (
        sum(
            1
            for issue in errors
            if issue.stage == "article_html" and issue.source_type == "media"
        )
        == 2
    )
    assert not any(
        issue.stage == "source→html" and issue.source_type == "media"
        for issue in errors
    )


def test_unresolved_marker_with_wrong_count_does_not_suppress_items() -> None:
    content_state = {
        "blocks": [_atomic("m1")],
        "entityMap": [
            {
                "key": "m1",
                "value": {
                    "type": "MEDIA",
                    "data": {
                        "mediaItems": [{"mediaId": "photo-a"}, {"mediaId": "photo-b"}]
                    },
                },
            }
        ],
    }
    html = '<figure data-fidelity="unresolved-media" data-media-count="1"></figure>'
    report = evaluate_export(
        _analysis(html, content_state=content_state, media_entities={}),
        None,
        None,
        None,
    )
    errors = report.errors()
    assert any(
        issue.stage == "source→html"
        and issue.source_type == "media"
        and "photo-b" in issue.message
        for issue in errors
    )


# ---------------------------------------------------------------------------
# Cross-kind ordering
# ---------------------------------------------------------------------------


def test_cross_kind_reordering_is_detected() -> None:
    content_state = {
        "blocks": [
            _block("header-two", "Section"),
            _block("unstyled", "Body text."),
        ],
        "entityMap": [],
    }
    html = "<p>Body text.</p><h2>Section</h2>"
    report = evaluate_export(_analysis(html, content_state=content_state), None, None, None)
    assert not report.is_complete()
    assert any(
        issue.stage == "source→html" and "order" in issue.message.lower()
        for issue in report.issues
    )


# ---------------------------------------------------------------------------
# Clean canonical round trip must stay clean
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("renderer", [False, True])
def test_canonical_article_round_trip_stays_clean(renderer: bool) -> None:
    article, html = _render(
        _block("unstyled", "Intro"),
        _block("header-two", "Section"),
        _block("unordered-list-item", "Item"),
        _block("blockquote", "Quote"),
        _atomic("md0"),
        entity_map=[
            {"key": "md0", "value": {"type": "MARKDOWN", "data": {"markdown": "```\ncode\n```"}}}
        ],
    )
    content_state = article["content_state"]
    if renderer:
        from x_media_downloader.documents import render_markdown

        md = render_markdown(_analysis(html, content_state=content_state), {})
        report = evaluate_export(_analysis(html, content_state=content_state), {}, md, None)
    else:
        report = evaluate_export(_analysis(html, content_state=content_state), None, None, None)
    assert report.is_complete(), [issue.message for issue in report.issues]
