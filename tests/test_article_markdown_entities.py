from __future__ import annotations

from x_media_downloader.article_html import to_html


def render(article: dict) -> str:
    return "".join(to_html(article))


def _article(
    blocks: list[dict] | None = None,
    entities: list[dict] | None = None,
    media: list[dict] | None = None,
) -> dict:
    return {
        "content_state": {
            "blocks": blocks
            or [{"type": "atomic", "text": " ", "entityRanges": [{"key": "x"}]}],
            "entityMap": entities or [],
        },
        "media_entities": media or [],
    }


# X Articles encode fenced code cards as atomic MARKDOWN entities.
def test_atomic_markdown_fence_renders_code_block() -> None:
    article = _article(
        entities=[
            {
                "key": "x",
                "value": {
                    "type": "MARKDOWN",
                    "data": {
                        "markdown": (
                            "```bash\n"
                            "mkdir ~/projects/multifactor-alpha\n"
                            "cd ~/projects/multifactor-alpha\n"
                            "```"
                        )
                    },
                },
            }
        ]
    )

    assert render(article) == (
        "<pre><code>"
        "mkdir ~/projects/multifactor-alpha\n"
        "cd ~/projects/multifactor-alpha"
        "</code></pre>\n"
    )


def test_multiple_atomic_markdown_entities_survive_in_order() -> None:
    article = {
        "content_state": {
            "blocks": [
                {"type": "unstyled", "text": "Intro."},
                {"type": "atomic", "text": " ", "entityRanges": [{"key": "one"}]},
                {"type": "unstyled", "text": "Middle paragraph."},
                {"type": "atomic", "text": " ", "entityRanges": [{"key": "two"}]},
                {"type": "atomic", "text": " ", "entityRanges": [{"key": "three"}]},
                {"type": "unstyled", "text": "Outro paragraph."},
            ],
            "entityMap": [
                {
                    "key": "one",
                    "value": {"type": "MARKDOWN", "data": {"markdown": "```bash\nfirst\n```"}},
                },
                {
                    "key": "two",
                    "value": {
                        "type": "MARKDOWN",
                        "data": {"markdown": "```python\nsecond = 2\n```"},
                    },
                },
                {
                    "key": "three",
                    "value": {"type": "MARKDOWN", "data": {"markdown": "```sh\nthird\n```"}},
                },
            ],
        },
        "media_entities": [],
    }

    assert render(article) == (
        "<p>Intro.</p>\n"
        "<pre><code>first</code></pre>\n"
        "<p>Middle paragraph.</p>\n"
        "<pre><code>second = 2</code></pre>\n"
        "<pre><code>third</code></pre>\n"
        "<p>Outro paragraph.</p>\n"
    )


def test_non_fenced_markdown_entity_is_preserved_with_marker() -> None:
    article = _article(
        [
            {"type": "atomic", "text": " ", "entityRanges": [{"key": "md"}]}
        ],
        [
            {
                "key": "md",
                "value": {
                    "type": "MARKDOWN",
                    "data": {"markdown": "# Unfenced heading\n\nSome markdown prose."},
                },
            }
        ],
    )

    html = render(article)

    assert 'data-fidelity="atomic-markdown"' in html
    assert html.startswith("<pre><code ")
    assert "# Unfenced heading" in html


def test_unknown_atomic_entity_type_is_kept_and_marked() -> None:
    article = _article(
        entities=[
            {
                "key": "x",
                "value": {
                    "type": "TIMELINE",
                    "data": {"text": "A tweet referenced here"},
                },
            }
        ]
    )

    html = render(article)

    assert 'data-fidelity="unsupported-entity"' in html
    assert 'data-entity-type="TIMELINE"' in html
    assert "A tweet referenced here" in html


def test_unknown_block_type_keeps_text_and_is_marked() -> None:
    article = {
        "content_state": {
            "blocks": [{"type": "table", "text": "cell one | cell two"}],
            "entityMap": [],
        },
        "media_entities": [],
    }

    html = render(article)

    assert 'data-fidelity="unsupported-block"' in html
    assert 'data-block-type="table"' in html
    assert "cell one | cell two" in html
    assert html.startswith("<p")


def test_atomic_block_missing_entity_map_key_is_marked() -> None:
    article = _article(
        [
            {
                "type": "atomic",
                "text": "fallback text",
                "entityRanges": [{"key": "ghost"}],
            }
        ]
    )

    html = render(article)

    assert 'data-fidelity="missing-entity"' in html
    assert "fallback text" in html


def test_atomic_markdown_code_characters_are_escaped() -> None:
    article = _article(
        entities=[
            {
                "key": "x",
                "value": {
                    "type": "MARKDOWN",
                    "data": {"markdown": '```html\n<script>alert("x&y");</script>\n```'},
                },
            }
        ]
    )

    html = render(article)

    assert "<script>" not in html
    assert "&lt;script&gt;alert(&quot;x&amp;y&quot;);&lt;/script&gt;" in html


def test_unresolved_media_entity_is_marked_and_caption_kept() -> None:
    article = _article(
        [
            {"type": "atomic", "text": " ", "entityRanges": [{"key": "media"}]}
        ],
        [
            {
                "key": "media",
                "value": {
                    "type": "MEDIA",
                    "data": {
                        "mediaItems": [{"mediaId": "broken"}],
                        "caption": "Kept caption",
                    },
                },
            }
        ],
        media=[{"media_id": "broken", "media_info": {}}],
    )

    html = render(article)

    assert 'data-fidelity="unresolved-media"' in html
    assert "Kept caption" in html


def test_resolved_media_entity_has_no_marker() -> None:
    article = _article(
        [
            {"type": "atomic", "text": " ", "entityRanges": [{"key": "media"}]}
        ],
        [
            {
                "key": "media",
                "value": {
                    "type": "MEDIA",
                    "data": {"mediaItems": [{"mediaId": "123"}], "caption": "Cap"},
                },
            }
        ],
        media=[
            {
                "media_id": "123",
                "media_info": {
                    "original_img_url": "https://pbs.twimg.com/media/photo.jpg",
                },
            }
        ],
    )

    html = render(article)

    assert "unresolved-media" not in html
    assert 'data-media-id="123"' in html
    assert "<figcaption>Cap</figcaption>" in html