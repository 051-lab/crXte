"""Raw article source extraction: classify X content_state constructs.

These helpers drive the fidelity "source" stages (``fidelity.scan_content_state``).
They mirror the classification used by :mod:`x_media_downloader.article_html` so
the same source construct maps to the same expected rendered shape, which is
what makes raw-to-rendered comparison sound.
"""

from __future__ import annotations

_BLOCK_KINDS = {
    "unstyled": "paragraph",
    "paragraph": "paragraph",
    "header-one": "heading",
    "header-two": "heading",
    "header-three": "heading",
    "header-four": "heading",
    "header-five": "heading",
    "header-six": "heading",
    "blockquote": "quote",
    "ordered-list-item": "list",
    "unordered-list-item": "list",
    "code-block": "code",
}

_HEADER_LEVELS = {
    "header-one": 1,
    "header-two": 2,
    "header-three": 3,
    "header-four": 4,
    "header-five": 5,
    "header-six": 6,
}

_ENTITY_KINDS = {
    "DIVIDER": "divider",
    "MARKDOWN": "code",
    "MEDIA": "media",
    "LATEX": "paragraph",
}

_ENTITY_PREVIEW_KEYS = ("text", "markdown", "formula", "caption", "title")


def extract_known_block_kinds() -> dict[str, str]:
    """Return the raw block types the renderer can represent natively."""
    return dict(_BLOCK_KINDS)


def header_level(block_type: str) -> int:
    """Map a raw header block type to its heading level."""
    try:
        return _HEADER_LEVELS[block_type]
    except KeyError:
        suffix = block_type.rsplit("-", 1)[-1]
        if suffix.isdigit():
            return int(suffix)
        raise ValueError(f"unsupported header block type: {block_type!r}") from None


def entity_kind(entity_type: str) -> str | None:
    return _ENTITY_KINDS.get(entity_type.upper())


def entity_preview(data: dict, block_text: str) -> str:
    """The text the renderer would keep for an atomic entity it cannot map."""
    for key in _ENTITY_PREVIEW_KEYS:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return (block_text or "").strip()


def extract_media_items(entity_data: dict) -> list[dict]:
    items = entity_data.get("mediaItems")
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]
