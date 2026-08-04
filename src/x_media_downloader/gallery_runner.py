from __future__ import annotations

import gallery_dl
from gallery_dl.extractor.utils import twitter_article

from .article_html import to_html


def main() -> int:
    twitter_article.to_html = to_html
    return gallery_dl.main()


if __name__ == "__main__":
    raise SystemExit(main())
