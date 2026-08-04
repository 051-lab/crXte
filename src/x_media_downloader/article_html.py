from __future__ import annotations

from bisect import bisect_left
from html import escape
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_STYLE_TAGS = {
    "BOLD": "b",
    "ITALIC": "i",
    "UNDERLINE": "u",
    "STRIKETHROUGH": "s",
    "MONOSPACE": "code",
    "CODE": "code",
}
_STYLE_ORDER = ("b", "i", "u", "s", "code")
_BLOCK_TAGS = {
    "unstyled": "p",
    "paragraph": "p",
    "header-one": "h1",
    "header-two": "h2",
    "header-three": "h3",
    "header-four": "h4",
    "header-five": "h5",
    "header-six": "h6",
    "blockquote": "blockquote",
}


def _integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _string(value: object) -> str:
    return value if isinstance(value, str) else ""


def _safe_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or any(ord(character) < 32 or ord(character) == 127 for character in value):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return None
        port = parsed.port
    except ValueError:
        return None
    if port is not None and not 1 <= port <= 65535:
        return None
    return value


def _media_url(value: object) -> str | None:
    url = _safe_url(value)
    if not url:
        return None
    parsed = urlsplit(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    suffix = parsed.path.rpartition(".")[2].lower()
    if suffix in {"gif", "jpeg", "jpg", "png", "webp"}:
        path = parsed.path.rpartition(".")[0]
        query["format"] = suffix
    else:
        path = parsed.path
    query["name"] = "orig"
    return urlunsplit((parsed.scheme, parsed.netloc, path, urlencode(query), parsed.fragment))


def _utf16_boundaries(value: str) -> list[int]:
    boundaries = [0]
    for character in value:
        boundaries.append(boundaries[-1] + (2 if ord(character) > 0xFFFF else 1))
    return boundaries


def _range_indices(
    boundaries: list[int], offset: object, length: object
) -> tuple[int, int] | None:
    start_units = _integer(offset)
    length_units = _integer(length)
    if start_units is None or length_units is None or start_units < 0 or length_units <= 0:
        return None
    total = boundaries[-1]
    end_units = start_units + length_units
    if start_units >= total or end_units > total:
        return None
    start = bisect_left(boundaries, start_units)
    end = bisect_left(boundaries, end_units)
    if boundaries[start] != start_units or boundaries[end] != end_units:
        return None
    return start, end


def _entity_map(content: dict) -> dict[str, dict]:
    raw = content.get("entityMap")
    if isinstance(raw, dict):
        return {str(key): value for key, value in raw.items() if isinstance(value, dict)}
    if not isinstance(raw, list):
        return {}
    entities: dict[str, dict] = {}
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("value"), dict):
            continue
        entities[str(item.get("key"))] = item["value"]
    return entities


def _media_map(article: dict) -> dict[str, dict]:
    raw = article.get("media_entities")
    if isinstance(raw, dict):
        values = raw.values()
    elif isinstance(raw, list):
        values = raw
    else:
        return {}
    return {
        str(item["media_id"]): item
        for item in values
        if isinstance(item, dict) and item.get("media_id") is not None
    }


def _link_target(entity: dict) -> str | None:
    data = entity.get("data")
    if not isinstance(data, dict):
        return None
    for key in ("url", "href", "expanded_url", "expandedUrl"):
        if target := _safe_url(data.get(key)):
            return target
    return None


def _mention_target(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    handle = value.strip().lstrip("@").strip()
    allowed = "_0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    if not handle or any(character not in allowed for character in handle):
        return None
    return f"https://x.com/{handle}"


def _inline_intervals(
    block: dict, entities: dict[str, dict], boundaries: list[int]
) -> tuple[list[tuple[int, int, str]], list[tuple[int, int, int, str]]]:
    styles: list[tuple[int, int, str]] = []
    links: list[tuple[int, int, int, str]] = []
    raw_styles = block.get("inlineStyleRanges")
    if isinstance(raw_styles, list):
        for item in raw_styles:
            if not isinstance(item, dict):
                continue
            tag = _STYLE_TAGS.get(_string(item.get("style")).upper())
            indices = _range_indices(boundaries, item.get("offset"), item.get("length"))
            if tag and indices:
                styles.append((*indices, tag))

    raw_ranges = block.get("entityRanges")
    if isinstance(raw_ranges, list):
        for order, item in enumerate(raw_ranges):
            if not isinstance(item, dict):
                continue
            entity = entities.get(str(item.get("key")))
            indices = _range_indices(boundaries, item.get("offset"), item.get("length"))
            if not entity or not indices:
                continue
            entity_type = _string(entity.get("type")).upper()
            target = _link_target(entity) if entity_type in {"LINK", "URL"} else None
            if entity_type in {"MENTION", "TWITTER_MENTION"}:
                data = entity.get("data")
                if isinstance(data, dict):
                    target = _mention_target(
                        data.get("screenName") or data.get("screen_name") or data.get("name")
                    )
            if target:
                links.append((*indices, order, target))

    data = block.get("data")
    if isinstance(data, dict):
        raw_urls = data.get("urls")
        if isinstance(raw_urls, list):
            for order, item in enumerate(raw_urls, len(links)):
                if not isinstance(item, dict):
                    continue
                start = item.get("fromIndex")
                end = item.get("toIndex")
                start_integer = _integer(start)
                end_integer = _integer(end)
                indices = (
                    _range_indices(boundaries, start_integer, end_integer - start_integer)
                    if start_integer is not None and end_integer is not None
                    else None
                )
                target = _safe_url(item.get("url") or item.get("expandedUrl") or item.get("text"))
                if indices and target:
                    links.append((*indices, order, target))
        raw_mentions = data.get("mentions")
        if isinstance(raw_mentions, list):
            for order, item in enumerate(raw_mentions, len(links)):
                if not isinstance(item, dict):
                    continue
                start = _integer(item.get("fromIndex"))
                end = _integer(item.get("toIndex"))
                indices = (
                    _range_indices(boundaries, start, end - start)
                    if start is not None and end is not None
                    else None
                )
                target = _mention_target(item.get("text"))
                if indices and target:
                    links.append((*indices, order, target))
    return styles, links


def _render_inline(block: dict, entities: dict[str, dict]) -> str:
    value = _string(block.get("text"))
    if not value:
        return ""
    utf16_boundaries = _utf16_boundaries(value)
    styles, links = _inline_intervals(block, entities, utf16_boundaries)
    boundaries = {0, len(value)}
    for start, end, _tag in styles:
        boundaries.update((start, end))
    for start, end, _order, _target in links:
        boundaries.update((start, end))

    runs: list[tuple[str | None, tuple[str, ...], str]] = []
    points = sorted(boundaries)
    for start, end in zip(points, points[1:], strict=False):
        active_styles = tuple(
            tag
            for tag in _STYLE_ORDER
            if any(
                left <= start and end <= right and candidate == tag
                for left, right, candidate in styles
            )
        )
        active_links = sorted(
            (
                (order, left, -(right - left), target)
                for left, right, order, target in links
                if left <= start and end <= right
            )
        )
        target = active_links[0][3] if active_links else None
        segment = value[start:end]
        if runs and runs[-1][:2] == (target, active_styles):
            previous_target, previous_styles, previous_value = runs[-1]
            runs[-1] = (previous_target, previous_styles, previous_value + segment)
        else:
            runs.append((target, active_styles, segment))

    rendered: list[str] = []
    open_tags: tuple[tuple[str, str | None], ...] = ()
    for target, active_styles, segment in runs:
        wanted = (
            (("a", target),) if target else ()
        ) + tuple((tag, None) for tag in active_styles)
        common = 0
        while common < min(len(open_tags), len(wanted)) and open_tags[common] == wanted[common]:
            common += 1
        rendered.extend(f"</{tag}>" for tag, _target in reversed(open_tags[common:]))
        for tag, tag_target in wanted[common:]:
            rendered.append(
                f'<a href="{escape(tag_target or "", quote=True)}">'
                if tag == "a"
                else f"<{tag}>"
            )
        rendered.append(escape(segment))
        open_tags = wanted
    rendered.extend(f"</{tag}>" for tag, _target in reversed(open_tags))
    return "".join(rendered)


def _media_item_html(media_id: str, media: dict) -> str:
    info = media.get("media_info")
    if not isinstance(info, dict):
        return ""
    escaped_id = escape(media_id, quote=True)
    if isinstance(info.get("variants"), list):
        variants = [
            item
            for item in info["variants"]
            if isinstance(item, dict) and _safe_url(item.get("url"))
        ]
        if not variants:
            return ""
        variant = max(variants, key=lambda item: _integer(item.get("bit_rate")) or 0)
        source = escape(_safe_url(variant.get("url")) or "", quote=True)
        preview = info.get("preview_image")
        poster = ""
        if isinstance(preview, dict) and (url := _media_url(preview.get("original_img_url"))):
            poster = f' poster="{escape(url, quote=True)}"'
        return f'<video controls data-media-id="{escaped_id}"{poster} src="{source}"></video>'
    source = _media_url(info.get("original_img_url"))
    if not source:
        return ""
    alt = _string(info.get("alt_text") or media.get("alt_text"))
    return (
        f'<img data-media-id="{escaped_id}" src="{escape(source, quote=True)}" '
        f'alt="{escape(alt, quote=True)}">'
    )


def _render_atomic(block: dict, entities: dict[str, dict], media: dict[str, dict]) -> str:
    raw_ranges = block.get("entityRanges")
    if not isinstance(raw_ranges, list):
        return ""
    rendered: list[str] = []
    for item in raw_ranges:
        if not isinstance(item, dict):
            continue
        entity = entities.get(str(item.get("key")))
        if not entity:
            continue
        entity_type = _string(entity.get("type")).upper()
        data = entity.get("data")
        data = data if isinstance(data, dict) else {}
        if entity_type == "DIVIDER":
            rendered.append("<hr>")
        elif entity_type == "LATEX":
            formula = _string(data.get("formula") or block.get("text"))
            rendered.append(
                '<math><semantics><annotation encoding="application/x-tex">'
                f"{escape(formula)}</annotation></semantics></math>"
            )
        elif entity_type == "MEDIA":
            raw_items = data.get("mediaItems")
            if not isinstance(raw_items, list):
                continue
            items: list[str] = []
            for media_item in raw_items:
                if not isinstance(media_item, dict) or media_item.get("mediaId") is None:
                    continue
                media_id = str(media_item["mediaId"])
                if media_html := _media_item_html(media_id, media.get(media_id, {})):
                    items.append(media_html)
            if items:
                caption = _string(data.get("caption"))
                caption_html = f"<figcaption>{escape(caption)}</figcaption>" if caption else ""
                rendered.append(f"<figure>{''.join(items)}{caption_html}</figure>")
    return "".join(rendered)


def _render_block(block: dict, entities: dict[str, dict], media: dict[str, dict]) -> str:
    block_type = _string(block.get("type"))
    if block_type == "atomic":
        return _render_atomic(block, entities, media)
    if block_type == "code-block":
        return f"<pre><code>{escape(_string(block.get('text')))}</code></pre>"
    tag = _BLOCK_TAGS.get(block_type, "p")
    return f"<{tag}>{_render_inline(block, entities)}</{tag}>"


def to_html(article: object) -> list[str]:
    if not isinstance(article, dict):
        return []
    content = article.get("content_state")
    if not isinstance(content, dict):
        return []
    raw_blocks = content.get("blocks")
    if not isinstance(raw_blocks, list):
        return []
    blocks = [block for block in raw_blocks if isinstance(block, dict)]
    entities = _entity_map(content)
    media = _media_map(article)
    rendered: list[str] = []
    index = 0
    while index < len(blocks):
        block = blocks[index]
        block_type = _string(block.get("type"))
        if block_type in {"ordered-list-item", "unordered-list-item"}:
            list_type = block_type
            items: list[str] = []
            while index < len(blocks) and blocks[index].get("type") == list_type:
                items.append(f"<li>{_render_inline(blocks[index], entities)}</li>")
                index += 1
            tag = "ol" if list_type == "ordered-list-item" else "ul"
            rendered.append(f"<{tag}>{''.join(items)}</{tag}>\n")
            continue
        rendered.append(_render_block(block, entities, media) + "\n")
        index += 1
    return rendered
