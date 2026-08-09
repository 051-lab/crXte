from __future__ import annotations

from x_media_downloader.article_html import to_html


# X Articles encode fenced code cards as atomic MARKDOWN entities.
def test_atomic_markdown_fence_renders_code_block() -> None:
    article = {
        "content_state": {
            "blocks": [
                {
                    "type": "atomic",
                    "text": " ",
                    "entityRanges": [{"key": "markdown", "offset": 0, "length": 1}],
                }
            ],
            "entityMap": [
                {
                    "key": "markdown",
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
            ],
        },
        "media_entities": [],
    }

    assert "".join(to_html(article)) == (
        "<pre><code>"
        "mkdir ~/projects/multifactor-alpha\n"
        "cd ~/projects/multifactor-alpha"
        "</code></pre>\n"
    )
