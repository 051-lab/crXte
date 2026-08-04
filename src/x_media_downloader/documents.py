from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape as html_escape
from importlib.resources import files
from io import BytesIO
from pathlib import Path
from urllib.parse import quote, urlsplit

from bs4 import BeautifulSoup, NavigableString, Tag

from .models import (
    Analysis,
    ArticleMetadata,
    Attachment,
    AttachmentRole,
    MediaType,
    OutputFormat,
    PostMetadata,
)


class DocumentError(RuntimeError):
    pass


@dataclass(frozen=True)
class DocumentMedia:
    attachment: Attachment
    path: Path
    reference: str | None = None


@dataclass(frozen=True)
class Text:
    value: str


@dataclass(frozen=True)
class Styled:
    kind: str
    children: tuple[Inline, ...]
    target: str | None = None


Inline = Text | Styled


@dataclass(frozen=True)
class Block:
    kind: str
    content: tuple[Inline, ...] = ()
    level: int = 0
    ordered: bool = False
    items: tuple[tuple[Inline, ...], ...] = ()
    start: int = 1
    code: str = ""
    media: DocumentMedia | None = None


class _MediaCursor:
    def __init__(self, media: Sequence[DocumentMedia], *, allow_legacy: bool) -> None:
        self.media = list(media)
        self.by_source_id = {
            item.attachment.source_id: item
            for item in media
            if item.attachment.source_id is not None
        }
        self.allow_legacy = allow_legacy
        self.position = 0
        self._used: set[Path] = set()

    def for_element(self, element: Tag) -> DocumentMedia | None:
        source_id = element.get("data-media-id")
        if isinstance(source_id, str) and source_id:
            item = self.by_source_id.get(source_id)
            if item:
                self._used.add(item.path)
            return item
        if not self.allow_legacy:
            return None
        while self.position < len(self.media):
            item = self.media[self.position]
            self.position += 1
            if item.path not in self._used:
                self._used.add(item.path)
                return item
        return None

    @property
    def used(self) -> set[Path]:
        return set(self._used)


def _safe_url(value: str | None) -> str | None:
    if not value or any(ord(character) < 32 for character in value):
        return None
    parsed = urlsplit(value.strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return None
    return quote(value.strip(), safe=":/?#@!$&'*+,;=%-._~")


def _normalize_text(value: str) -> str:
    return " ".join(value.split())


def _normalize_inline_text(value: str) -> str:
    normalized = _normalize_text(value)
    if not normalized:
        return " " if value else ""
    if value[:1].isspace():
        normalized = f" {normalized}"
    if value[-1:].isspace():
        normalized = f"{normalized} "
    return normalized


def _append_inline(result: list[Inline], node: Inline) -> None:
    if isinstance(node, Text) and result and isinstance(result[-1], Text):
        value = re.sub(r" +", " ", result[-1].value + node.value)
        result[-1] = Text(re.sub(r" *\n *", "\n", value))
    else:
        result.append(node)


def _trim_inline_spaces(nodes: list[Inline]) -> tuple[bool, bool]:
    leading = bool(nodes and isinstance(nodes[0], Text) and nodes[0].value.startswith(" "))
    trailing = bool(nodes and isinstance(nodes[-1], Text) and nodes[-1].value.endswith(" "))
    if leading:
        nodes[0] = Text(nodes[0].value.lstrip(" "))
    if trailing and nodes:
        nodes[-1] = Text(nodes[-1].value.rstrip(" "))
    nodes[:] = [node for node in nodes if not isinstance(node, Text) or node.value]
    return leading, trailing


def _normalize_inline_nodes(nodes: Sequence[Inline], *, trim: bool) -> tuple[Inline, ...]:
    result: list[Inline] = []
    for node in nodes:
        if isinstance(node, Text):
            value = re.sub(r" +", " ", node.value)
            value = re.sub(r" *\n *", "\n", value)
            if value:
                _append_inline(result, Text(value))
            continue
        children = list(_normalize_inline_nodes(node.children, trim=False))
        leading, trailing = _trim_inline_spaces(children)
        if leading:
            _append_inline(result, Text(" "))
        if children:
            _append_inline(result, Styled(node.kind, tuple(children), node.target))
        if trailing:
            _append_inline(result, Text(" "))
    if trim:
        _trim_inline_spaces(result)
    return tuple(result)


def _collect_inline_nodes(nodes: Iterable[object]) -> list[Inline]:
    result: list[Inline] = []
    for node in nodes:
        if isinstance(node, NavigableString):
            value = _normalize_inline_text(str(node))
            if value:
                _append_inline(result, Text(value))
            continue
        if not isinstance(node, Tag):
            continue
        name = node.name.lower()
        if name in {"script", "style", "iframe", "object", "embed", "svg"}:
            continue
        if name == "br":
            _append_inline(result, Text("\n"))
            continue
        children = tuple(_collect_inline_nodes(node.children))
        if not children:
            continue
        if name in {"strong", "b"}:
            _append_inline(result, Styled("strong", children))
        elif name in {"em", "i"}:
            _append_inline(result, Styled("emphasis", children))
        elif name == "code":
            _append_inline(result, Styled("code", children))
        elif name == "a":
            target = _safe_url(node.get("href"))
            _append_inline(
                result,
                Styled("link", children, target) if target else Styled("plain", children),
            )
        else:
            for child in children:
                _append_inline(result, child)
    return result


def _inline_nodes(nodes: Iterable[object]) -> tuple[Inline, ...]:
    return _normalize_inline_nodes(_collect_inline_nodes(nodes), trim=True)


def _inline_text(nodes: Sequence[Inline]) -> str:
    parts: list[str] = []
    for node in nodes:
        if isinstance(node, Text):
            parts.append(node.value)
        else:
            parts.append(_inline_text(node.children))
    return " ".join("".join(parts).split())


def _paragraph_blocks(tag: Tag, cursor: _MediaCursor) -> list[Block]:
    blocks: list[Block] = []
    pending: list[object] = []

    def flush() -> None:
        content = _inline_nodes(pending)
        if _inline_text(content):
            blocks.append(Block("paragraph", content))
        pending.clear()

    for child in tag.children:
        if isinstance(child, Tag):
            elements = (
                [child]
                if child.name.lower() in {"img", "video"}
                else child.find_all(["img", "video"])
            )
            if elements:
                flush()
                content = _inline_nodes([child])
                if _inline_text(content):
                    blocks.append(Block("paragraph", content))
                for element in elements:
                    media = cursor.for_element(element)
                    if media:
                        kind = (
                            "image"
                            if media.attachment.media_type == MediaType.PHOTO
                            else "video"
                        )
                        blocks.append(Block(kind, media=media))
                continue
        pending.append(child)
    flush()
    return blocks


def _blocks_from_tag(tag: Tag, cursor: _MediaCursor) -> list[Block]:
    name = tag.name.lower()
    if name in {"script", "style", "iframe", "object", "embed", "svg", "noscript"}:
        return []
    if name in {f"h{level}" for level in range(1, 7)}:
        content = _inline_nodes(tag.children)
        return [Block("heading", content, level=int(name[1]))] if _inline_text(content) else []
    if name == "p":
        return _paragraph_blocks(tag, cursor)
    if name == "blockquote":
        content = _inline_nodes(tag.children)
        return [Block("quote", content)] if _inline_text(content) else []
    if name in {"ul", "ol"}:
        items = []
        for item in tag.find_all("li", recursive=False):
            content = _inline_nodes(
                child
                for child in item.children
                if not isinstance(child, Tag) or child.name not in {"ul", "ol"}
            )
            if _inline_text(content):
                items.append(content)
        start = 1
        if name == "ol":
            try:
                start = int(tag.get("start", 1))
            except (TypeError, ValueError):
                start = 1
        if not items:
            return []
        return [Block("list", ordered=name == "ol", items=tuple(items), start=start)]
    if name == "pre":
        return [Block("code", code=tag.get_text("", strip=False).strip("\n"))]
    if name == "hr":
        return [Block("divider")]
    if name in {"img", "video"}:
        media = cursor.for_element(tag)
        if not media:
            return []
        kind = "image" if media.attachment.media_type == MediaType.PHOTO else "video"
        return [Block(kind, media=media)]
    if name == "figure":
        blocks: list[Block] = []
        for element in tag.find_all(["img", "video"]):
            blocks.extend(_blocks_from_tag(element, cursor))
        caption = tag.find("figcaption")
        if caption:
            content = _inline_nodes(caption.children)
            if _inline_text(content):
                blocks.append(Block("caption", content))
        return blocks
    if name == "table":
        blocks = []
        for row in tag.find_all("tr"):
            cells = [
                _normalize_text(cell.get_text(" "))
                for cell in row.find_all(["th", "td"], recursive=False)
            ]
            if cells:
                blocks.append(Block("paragraph", (Text(" | ".join(cells)),)))
        return blocks
    blocks = []
    for child in tag.children:
        if isinstance(child, Tag):
            blocks.extend(_blocks_from_tag(child, cursor))
        elif isinstance(child, NavigableString):
            value = _normalize_text(str(child))
            if value:
                blocks.append(Block("paragraph", (Text(value),)))
    return blocks


def _is_body_media(item: DocumentMedia) -> bool:
    return item.attachment.role in {
        AttachmentRole.ARTICLE_IMAGE,
        AttachmentRole.ARTICLE_VIDEO,
    }


def _is_cover_media(item: DocumentMedia) -> bool:
    return item.attachment.role == AttachmentRole.ARTICLE_COVER


def _article_blocks(
    html: str,
    media: Sequence[DocumentMedia],
    *,
    allow_legacy_media: bool,
) -> tuple[list[Block], set[Path]]:
    cursor = _MediaCursor(
        [item for item in media if _is_body_media(item)],
        allow_legacy=allow_legacy_media,
    )
    soup = BeautifulSoup(html, "html.parser")
    root = soup.body or soup
    blocks: list[Block] = []
    for child in root.children:
        if isinstance(child, Tag):
            blocks.extend(_blocks_from_tag(child, cursor))
        elif isinstance(child, NavigableString):
            value = _normalize_text(str(child))
            if value:
                blocks.append(Block("paragraph", (Text(value),)))
    return blocks, cursor.used


def _markdown_escape(value: str) -> str:
    for character in "\\`*_{}[]<>#|":
        value = value.replace(character, f"\\{character}")
    return value


def _markdown_inline(nodes: Sequence[Inline]) -> str:
    rendered: list[str] = []
    for node in nodes:
        if isinstance(node, Text):
            rendered.append(_markdown_escape(node.value).replace("\n", "  \n"))
            continue
        value = _markdown_inline(node.children)
        if node.kind == "strong":
            rendered.append(f"**{value}**")
        elif node.kind == "emphasis":
            rendered.append(f"*{value}*")
        elif node.kind == "code":
            rendered.append(f"`{value.replace('`', '\\`')}`")
        elif node.kind == "link" and node.target:
            rendered.append(f"[{value}](<{node.target}>)")
        else:
            rendered.append(value)
    return "".join(rendered)


def _media_alt(item: DocumentMedia) -> str:
    return getattr(item.attachment, "alt_text", None) or item.path.stem


def _markdown_media_target(item: DocumentMedia) -> str:
    value = item.reference or item.path.name
    return "/".join(quote(part, safe="@-._~") for part in Path(value).parts)


def document_base(analysis: Analysis) -> str:
    handle = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "_", analysis.post.author_handle).strip(" .")
    handle = re.sub(r"\s+", "_", handle)[:80] or "x"
    post_id = re.sub(r"[^A-Za-z0-9_-]", "_", analysis.post.post_id)[:80] or "post"
    return f"@{handle}_{post_id}"


def markdown_assets_name(analysis: Analysis) -> str:
    return f"{document_base(analysis)}_assets"


def _analysis_article(analysis: Analysis) -> ArticleMetadata | None:
    return analysis.article


def _analysis_media(analysis: Analysis, media_paths: dict[str, Path]) -> list[DocumentMedia]:
    result = []
    for attachment in analysis.attachments:
        path = media_paths.get(attachment.id)
        if path:
            reference = (
                str(Path(path.parent.name) / path.name)
                if path.parent.name in {"media", markdown_assets_name(analysis)}
                else path.name
            )
            result.append(DocumentMedia(attachment, path, reference))
    return result


def _markdown_blocks(blocks: Sequence[Block]) -> list[str]:
    rendered: list[str] = []
    for block in blocks:
        if block.kind == "heading":
            rendered.append(f"{'#' * min(6, block.level + 1)} {_markdown_inline(block.content)}")
        elif block.kind == "paragraph":
            rendered.append(_markdown_inline(block.content))
        elif block.kind == "caption":
            rendered.append(f"*{_markdown_inline(block.content)}*")
        elif block.kind == "quote":
            rendered.append(
                "\n".join(f"> {line}" for line in _markdown_inline(block.content).splitlines())
            )
        elif block.kind == "list":
            lines = []
            for index, item in enumerate(block.items, block.start):
                marker = f"{index}." if block.ordered else "-"
                lines.append(f"{marker} {_markdown_inline(item)}")
            rendered.append("\n".join(lines))
        elif block.kind == "code":
            rendered.append(f"```\n{block.code.replace('```', '` ` `')}\n```")
        elif block.kind == "divider":
            rendered.append("---")
        elif block.kind in {"image", "video"} and block.media:
            label = _markdown_escape(_media_alt(block.media))
            target = _markdown_media_target(block.media)
            rendered.append(
                f"![{label}](<{target}>)"
                if block.kind == "image"
                else f"[{label}](<{target}>)"
            )
    return rendered


def _document_content(
    post: PostMetadata,
    article: ArticleMetadata | None,
    media: Sequence[DocumentMedia],
) -> tuple[str, list[Block], list[DocumentMedia]]:
    if article:
        title = article.title or f"Post by @{post.author_handle}"
        blocks, used = _article_blocks(
            article.html,
            media,
            allow_legacy_media=article.html_renderer_version == 0,
        )
        remaining = [item for item in media if item.path not in used]
        return title, blocks, remaining
    title = f"Post by @{post.author_handle}"
    blocks = [Block("paragraph", (Text(post.text),))] if post.text else []
    return title, blocks, list(media)


def render_markdown_text(
    post: PostMetadata,
    media: Sequence[DocumentMedia],
    *,
    article: ArticleMetadata | None = None,
    source_url: str | None = None,
) -> str:
    title, blocks, remaining = _document_content(post, article, media)
    lines = [
        f"# {_markdown_escape(title)}",
        "",
        f"By {_markdown_escape(post.author_name)} (@{_markdown_escape(post.author_handle)})",
    ]
    published_at = article.published_at if article else post.posted_at
    if published_at:
        lines.append(f"Published: {_markdown_escape(published_at)}")
    safe_source = _safe_url(source_url)
    if safe_source:
        lines.append(f"Source: <{safe_source}>")
    lines.append("")
    covers = [item for item in remaining if _is_cover_media(item)]
    remaining = [item for item in remaining if not _is_cover_media(item)]
    lines.extend(_markdown_blocks([Block("image", media=item) for item in covers]))
    if covers and blocks:
        lines.append("")
    lines.extend(_markdown_blocks(blocks))
    if remaining:
        if blocks:
            lines.extend(["", "## Attachments"])
        for item in remaining:
            target = _markdown_media_target(item)
            label = _markdown_escape(_media_alt(item))
            lines.extend(
                [
                    "",
                    f"![{label}](<{target}>)"
                    if item.attachment.media_type == MediaType.PHOTO
                    else f"[{label}](<{target}>)",
                ]
            )
    return "\n".join(lines).rstrip() + "\n"


def render_markdown(analysis: Analysis, media_paths: dict[str, Path]) -> bytes:
    try:
        media = _analysis_media(analysis, media_paths)
        return render_markdown_text(
            analysis.post,
            media,
            article=_analysis_article(analysis),
            source_url=analysis.url,
        ).encode("utf-8")
    except DocumentError:
        raise
    except Exception as error:
        raise DocumentError("Could not render the Markdown document.") from error


def write_markdown(
    path: Path,
    post: PostMetadata,
    media: Sequence[DocumentMedia],
    *,
    article: ArticleMetadata | None = None,
    source_url: str | None = None,
) -> int:
    content = render_markdown_text(post, media, article=article, source_url=source_url)
    path.write_text(content, encoding="utf-8", newline="\n")
    return path.stat().st_size


def _document_font() -> tuple[str, set[int]]:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    name = "XMediaDejaVuSans"
    if name not in pdfmetrics.getRegisteredFontNames():
        font_path = files("x_media_downloader").joinpath("assets/DejaVuSans.ttf")
        pdfmetrics.registerFont(TTFont(name, str(font_path)))
    font = pdfmetrics.getFont(name)
    return name, set(font.face.charWidths)


def _supported_text(value: str, supported: set[int]) -> str:
    replacement = "□" if ord("□") in supported else "?"
    return "".join(
        character if ord(character) in supported or character in "\n\t" else replacement
        for character in value
    )


def _pdf_inline(nodes: Sequence[Inline], supported: set[int]) -> str:
    rendered: list[str] = []
    for node in nodes:
        if isinstance(node, Text):
            rendered.append(
                html_escape(_supported_text(node.value, supported)).replace("\n", "<br/>")
            )
            continue
        value = _pdf_inline(node.children, supported)
        if node.kind == "strong":
            rendered.append(f"<b>{value}</b>")
        elif node.kind == "emphasis":
            rendered.append(f"<i>{value}</i>")
        elif node.kind == "code":
            rendered.append(value)
        elif node.kind == "link" and node.target:
            rendered.append(f'<link href="{html_escape(node.target, quote=True)}">{value}</link>')
        else:
            rendered.append(value)
    return "".join(rendered)


def _reportlab_date_formatter(
    value: str | None,
) -> Callable[[int, int, int, int, int, int], str] | None:
    if not value:
        return None
    try:
        parsed = (
            datetime.strptime(value, "%Y%m%d").replace(tzinfo=UTC)
            if re.fullmatch(r"\d{8}", value)
            else datetime.fromisoformat(value.replace("Z", "+00:00"))
        )
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    offset = parsed.utcoffset()
    offset_minutes = 0 if offset is None else int(offset.total_seconds() // 60)
    sign = "+" if offset_minutes >= 0 else "-"
    offset_hours, offset_remainder = divmod(abs(offset_minutes), 60)
    formatted = (
        f"D:{parsed:%Y%m%d%H%M%S}{sign}{offset_hours:02d}'{offset_remainder:02d}'"
    )

    def format_date(
        _year: int,
        _month: int,
        _day: int,
        _hour: int,
        _minute: int,
        _second: int,
    ) -> str:
        return formatted

    return format_date


def _build_pdf(
    destination: Path | BytesIO,
    post: PostMetadata,
    media: Sequence[DocumentMedia],
    *,
    article: ArticleMetadata | None = None,
    source_url: str | None = None,
) -> None:
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.pdfgen.canvas import Canvas
    from reportlab.platypus import (
        HRFlowable,
        Image,
        ListFlowable,
        ListItem,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
    )

    font_name, supported = _document_font()
    styles = getSampleStyleSheet()
    body = ParagraphStyle(
        "DocumentBody",
        parent=styles["BodyText"],
        fontName=font_name,
        fontSize=10.5,
        leading=15,
        spaceAfter=8,
    )
    title_style = ParagraphStyle(
        "DocumentTitle", parent=body, fontSize=22, leading=27, spaceAfter=10
    )
    metadata_style = ParagraphStyle(
        "DocumentMetadata", parent=body, fontSize=9, textColor="#555555", spaceAfter=4
    )
    quote_style = ParagraphStyle(
        "DocumentQuote",
        parent=body,
        leftIndent=12,
        borderColor="#999999",
        borderWidth=1,
        borderPadding=6,
    )
    caption_style = ParagraphStyle(
        "DocumentCaption", parent=body, fontSize=8.5, alignment=TA_CENTER, textColor="#555555"
    )
    heading_styles = {
        level: ParagraphStyle(
            f"DocumentH{level}",
            parent=body,
            fontSize=max(12, 20 - level * 2),
            leading=max(15, 24 - level * 2),
            spaceBefore=8,
        )
        for level in range(1, 7)
    }

    title, blocks, remaining = _document_content(post, article, media)
    author_metadata = f"{post.author_name} (@{post.author_handle})"
    published_at = article.published_at if article else post.posted_at
    safe_source = _safe_url(source_url)
    subject = f"Source: {safe_source}" if safe_source else "X article" if article else "X post"
    keywords = f"X, crXte, {'article' if article else 'post'}, @{post.author_handle}"
    date_formatter = _reportlab_date_formatter(published_at)
    story = [Paragraph(html_escape(_supported_text(title, supported)), title_style)]
    author = f"By {author_metadata}"
    story.append(Paragraph(html_escape(_supported_text(author, supported)), metadata_style))
    if published_at:
        story.append(
            Paragraph(
                f"Published: {html_escape(_supported_text(published_at, supported))}",
                metadata_style,
            )
        )
    if safe_source:
        story.append(
            Paragraph(
                f'<link href="{html_escape(safe_source, quote=True)}">Source</link>', metadata_style
            )
        )
    story.append(Spacer(1, 5 * mm))

    def add_media(item: DocumentMedia) -> None:
        if item.attachment.media_type == MediaType.PHOTO and item.path.is_file():
            image = Image(str(item.path))
            image._restrictSize(170 * mm, 190 * mm)
            story.extend(
                [
                    image,
                    Paragraph(
                        html_escape(_supported_text(_media_alt(item), supported)), caption_style
                    ),
                    Spacer(1, 3 * mm),
                ]
            )
        else:
            target = html_escape(_markdown_media_target(item), quote=True)
            label = html_escape(_supported_text(_media_alt(item), supported))
            story.append(Paragraph(f'Video: <link href="{target}">{label}</link>', body))

    covers = [item for item in remaining if _is_cover_media(item)]
    remaining = [item for item in remaining if not _is_cover_media(item)]
    for cover in covers:
        add_media(cover)

    for block in blocks:
        if block.kind == "heading":
            story.append(
                Paragraph(_pdf_inline(block.content, supported), heading_styles[block.level])
            )
        elif block.kind == "paragraph":
            story.append(Paragraph(_pdf_inline(block.content, supported), body))
        elif block.kind == "caption":
            story.append(Paragraph(_pdf_inline(block.content, supported), caption_style))
        elif block.kind == "quote":
            story.append(Paragraph(_pdf_inline(block.content, supported), quote_style))
        elif block.kind == "list":
            items = [
                ListItem(Paragraph(_pdf_inline(item, supported), body)) for item in block.items
            ]
            list_options: dict[str, object] = {
                "bulletType": "1" if block.ordered else "bullet"
            }
            if block.ordered:
                list_options["start"] = block.start
            story.append(ListFlowable(items, **list_options))
            story.append(Spacer(1, 2 * mm))
        elif block.kind == "code":
            story.append(
                Paragraph(
                    html_escape(_supported_text(block.code, supported)).replace("\n", "<br/>"), body
                )
            )
        elif block.kind == "divider":
            story.append(
                HRFlowable(
                    width="100%", thickness=0.5, color="#999999", spaceBefore=5, spaceAfter=8
                )
            )
        elif block.kind in {"image", "video"} and block.media:
            add_media(block.media)
    if remaining:
        if blocks:
            story.append(Paragraph("Attachments", heading_styles[1]))
        for item in remaining:
            add_media(item)

    document = SimpleDocTemplate(
        str(destination) if isinstance(destination, Path) else destination,
        pagesize=A4,
        rightMargin=20 * mm,
        leftMargin=20 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
    )

    def invariant_canvas(filename: str, **kwargs: object) -> Canvas:
        kwargs["invariant"] = 1
        kwargs["pageCompression"] = 1
        canvas = Canvas(filename, **kwargs)
        if date_formatter:
            canvas.setDateFormatter(date_formatter)
        return canvas

    def set_metadata(canvas: Canvas, _document: object) -> None:
        canvas.setTitle(title)
        canvas.setAuthor(author_metadata)
        canvas.setSubject(subject)
        canvas.setCreator("crXte")
        canvas.setKeywords(keywords)

    document.build(story, onFirstPage=set_metadata, canvasmaker=invariant_canvas)


def write_pdf(
    path: Path,
    post: PostMetadata,
    media: Sequence[DocumentMedia],
    *,
    article: ArticleMetadata | None = None,
    source_url: str | None = None,
) -> int:
    _build_pdf(path, post, media, article=article, source_url=source_url)
    return path.stat().st_size


def render_pdf(analysis: Analysis, media_paths: dict[str, Path]) -> bytes:
    try:
        output = BytesIO()
        _build_pdf(
            output,
            analysis.post,
            _analysis_media(analysis, media_paths),
            article=_analysis_article(analysis),
            source_url=analysis.url,
        )
        return output.getvalue()
    except DocumentError:
        raise
    except Exception as error:
        raise DocumentError("Could not render the PDF document.") from error


def write_document(
    path: Path,
    output_format: OutputFormat,
    post: PostMetadata,
    media: Sequence[DocumentMedia],
    *,
    article: ArticleMetadata | None = None,
    source_url: str | None = None,
) -> int:
    if output_format == OutputFormat.MARKDOWN:
        return write_markdown(path, post, media, article=article, source_url=source_url)
    if output_format == OutputFormat.PDF:
        return write_pdf(path, post, media, article=article, source_url=source_url)
    raise ValueError(f"Unsupported document format: {output_format}")
