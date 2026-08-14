from __future__ import annotations

import json
import os
from pathlib import Path

import gallery_dl
from gallery_dl.extractor.utils import twitter_article

from .article_html import to_html


def _capture_raw_article(article: object) -> None:
    """Persist the renderer's input so fidelity can audit the raw source.

    gallery-dl strips ``content_state`` and ``media_entities`` from the article
    metadata it emits, so the patched renderer records its own input to the
    path given in ``CR_XTE_RAW_CAPTURE``.  The extractor reads that file back
    and attaches it to the analysis.
    """
    if not isinstance(article, dict):
        return
    path = os.environ.get("CR_XTE_RAW_CAPTURE")
    if not path:
        return
    payload = {
        "content_state": article.get("content_state"),
        "media_entities": article.get("media_entities"),
    }
    try:
        Path(path).write_text(json.dumps(payload, ensure_ascii=False))
    except OSError:
        return


def _capturing_to_html(article: object) -> list[str]:
    _capture_raw_article(article)
    return to_html(article)


def main() -> int:
    twitter_article.to_html = _capturing_to_html
    return gallery_dl.main()


if __name__ == "__main__":
    raise SystemExit(main())
