from __future__ import annotations

from copy import deepcopy

from gallery_dl.extractor.utils import twitter_article

from x_media_downloader import gallery_runner
from x_media_downloader.article_html import to_html


def render(*blocks: dict, entities: object = None, media: object = None) -> str:
    article = {
        "content_state": {"blocks": list(blocks), "entityMap": entities or []},
        "media_entities": media or [],
    }
    return "".join(to_html(article))


def test_style_ranges_preserve_contractions_and_do_not_mutate_input() -> None:
    article = {
        "content_state": {
            "entityMap": [],
            "blocks": [
                {
                    "key": "one",
                    "type": "unstyled",
                    "text": "They don’t route",
                    "inlineStyleRanges": [{"offset": 6, "length": 4, "style": "BOLD"}],
                },
                {
                    "key": "two",
                    "type": "unstyled",
                    "text": "An edge isn’t enough",
                    "inlineStyleRanges": [{"offset": 11, "length": 2, "style": "ITALIC"}],
                },
            ],
        },
        "media_entities": [],
    }
    original = deepcopy(article)

    assert "".join(to_html(article)) == (
        "<p>They d<b>on’t</b> route</p>\n<p>An edge isn<i>’t</i> enough</p>\n"
    )
    assert article == original


def test_utf16_offsets_account_for_astral_emoji() -> None:
    html = render(
        {
            "type": "unstyled",
            "text": "🙂bold text",
            "inlineStyleRanges": [{"offset": 2, "length": 4, "style": "BOLD"}],
        }
    )

    assert html == "<p>🙂<b>bold</b> text</p>\n"


def test_crossing_styles_and_links_are_valid_and_deterministic() -> None:
    html = render(
        {
            "type": "unstyled",
            "text": "abcdef",
            "inlineStyleRanges": [
                {"offset": 1, "length": 4, "style": "BOLD"},
                {"offset": 3, "length": 3, "style": "ITALIC"},
            ],
            "entityRanges": [{"offset": 2, "length": 3, "key": 0}],
        },
        entities={
            "0": {"type": "LINK", "data": {"url": "https://example.com/?a=1&b=2"}}
        },
    )

    assert html == (
        '<p>a<b>b</b><a href="https://example.com/?a=1&amp;b=2"><b>c<i>de</i></b></a>'
        "<i>f</i></p>\n"
    )


def test_text_is_escaped_and_unsafe_link_is_not_emitted() -> None:
    html = render(
        {
            "type": "unstyled",
            "text": '<script>& "click"',
            "entityRanges": [{"offset": 10, "length": 7, "key": "unsafe"}],
        },
        entities=[
            {
                "key": "unsafe",
                "value": {"type": "LINK", "data": {"url": "javascript:alert(1)"}},
            }
        ],
    )

    assert html == "<p>&lt;script&gt;&amp; &quot;click&quot;</p>\n"
    assert "href" not in html


def test_semantic_blocks_lists_and_media_ids() -> None:
    entities = [
        {
            "key": "media",
            "value": {
                "type": "MEDIA",
                "data": {
                    "mediaItems": [{"mediaId": "123"}],
                    "caption": "Caption <unsafe>",
                },
            },
        },
        {"key": "divider", "value": {"type": "DIVIDER", "data": {}}},
    ]
    media = [
        {
            "media_id": "123",
            "media_info": {
                "original_img_url": "https://pbs.twimg.com/media/photo.jpg",
                "alt_text": 'Alt "text"',
            },
        }
    ]
    html = render(
        {"type": "header-two", "text": "Heading"},
        {"type": "unordered-list-item", "text": "First"},
        {"type": "unordered-list-item", "text": "Second"},
        {"type": "atomic", "text": " ", "entityRanges": [{"key": "media"}]},
        {"type": "atomic", "text": " ", "entityRanges": [{"key": "divider"}]},
        entities=entities,
        media=media,
    )

    assert html == (
        "<h2>Heading</h2>\n"
        "<ul><li>First</li><li>Second</li></ul>\n"
        '<figure><img data-media-id="123" '
        'src="https://pbs.twimg.com/media/photo?format=jpg&amp;name=orig" '
        'alt="Alt &quot;text&quot;"><figcaption>Caption &lt;unsafe&gt;</figcaption></figure>\n'
        "<hr>\n"
    )


def test_runner_patches_article_html_and_delegates(monkeypatch) -> None:
    sentinel = object()
    original_process_text = twitter_article.process_text
    monkeypatch.setattr(twitter_article, "to_html", sentinel)
    monkeypatch.setattr(gallery_runner.gallery_dl, "main", lambda: 17)

    assert gallery_runner.main() == 17
    assert twitter_article.to_html is to_html
    assert twitter_article.process_text is original_process_text


def test_invalid_ranges_are_ignored_without_clamping() -> None:
    html = render(
        {
            "type": "unstyled",
            "text": "🙂plain",
            "inlineStyleRanges": [
                {"offset": 1, "length": 1, "style": "BOLD"},
                {"offset": 4, "length": 99, "style": "ITALIC"},
            ],
        }
    )

    assert html == "<p>🙂plain</p>\n"


def test_invalid_content_is_ignored() -> None:
    assert to_html(None) == []
    assert to_html({"content_state": {"blocks": "not-a-list"}}) == []
