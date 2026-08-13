"""Export fidelity: detect and report silent source content loss.

crXte renders X Articles through a pipeline of canonical forms:

    content_state ── article_html.to_html ──▶ source HTML
    source HTML ──── documents._article_blocks ──▶ canonical Blocks
    canonical Blocks ──────────────────────────▶ Markdown and PDF

This module makes every boundary measurable.  The article renderer marks any
construct it could not represent faithfully with ``data-fidelity`` attributes,
and the structural record derived from the source HTML is compared against the
canonical document blocks and the produced Markdown/PDF output.

Severity contract:

* ``info``    – internal observation, no user impact
* ``warning`` – content preserved, but structure or presentation changed
* ``error``   – supported source content was not represented at all

An export is only considered fully successful when its report contains no
``error`` issues.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from bs4 import BeautifulSoup, Tag
from pypdf import PdfReader

from .documents import (
    Block,
    DocumentMedia,
    Text,
    _analysis_media,
    _markdown_media_target,
    document_blocks,
)
from .extract_extractors import (
    entity_kind,
    entity_preview,
    extract_known_block_kinds,
    extract_media_items,
    header_level,
)
from .models import Analysis
from .models import FidelityIssue as JobIssue

_WARNING_MARKERS = {
    "unsupported-block",
    "unsupported-entity",
    "atomic-markdown",
}
_ERROR_MARKERS = {
    "unresolved-media",
    "missing-entity",
}
_ALL_MARKERS = _WARNING_MARKERS | _ERROR_MARKERS

_MARKDOWN_ESCAPED = "\\`*_{}[]<>#|"
_LINK_TARGET = re.compile(r"!?\[([^\]]*)\]\(<([^>]*)>\)")
_STRONG = re.compile(r"\*\*(.+?)\*\*")
_EMPHASIS = re.compile(r"\*([^*]+)\*")
_INLINE_CODE = re.compile(r"`([^`]*)`")
_ESCAPE = re.compile(r"\\([" + re.escape(_MARKDOWN_ESCAPED) + r"])")
_FENCE = re.compile(r"^```$|^```[A-Za-z0-9_-]+$")
_ORDERED_ITEM = re.compile(r"^(\d+)\.\s+(.*)$")
_UNORDERED_ITEM = re.compile(r"^[-*]\s+(.*)$")
_QUOTE_LINE = re.compile(r"^>\s?(.*)$")
_DIVIDER_LINE = re.compile(r"^---+$|^\*\*\*+$")
_HEADING_LINE = re.compile(r"^(#{1,6})\s+(.*)$")
_MEDIA_TARGET = re.compile(r"\]\(<([^>]*)>\)")


def _fold(value: str) -> str:
    return " ".join(value.split())


def _preview(value: str, limit: int = 80) -> str | None:
    folded = _fold(value)
    if not folded:
        return None
    return folded[:limit] + ("…" if len(folded) > limit else "")


@dataclass(frozen=True, slots=True)
class FidelityIssue:
    severity: str
    stage: str
    source_type: str
    block_index: int | None = None
    entity_type: str | None = None
    message: str = ""
    content_preview: str | None = None
    media_count: int | None = None


@dataclass(frozen=True, slots=True)
class FidelityReport:
    issues: tuple[FidelityIssue, ...] = ()

    def errors(self) -> tuple[FidelityIssue, ...]:
        return tuple(issue for issue in self.issues if issue.severity == "error")

    def warnings(self) -> tuple[FidelityIssue, ...]:
        return tuple(issue for issue in self.issues if issue.severity == "warning")

    def is_complete(self) -> bool:
        return not self.errors()

    @property
    def issue_count(self) -> int:
        return len(self.issues)

    def summary(self) -> str:
        if not self.issues:
            return "no fidelity issues"
        parts = []
        if len(self.errors()):
            parts.append(f"{len(self.errors())} content issue(s)")
        if len(self.warnings()):
            parts.append(f"{len(self.warnings())} warning(s)")
        return ", ".join(parts)


def _merge(*reports: FidelityReport) -> FidelityReport:
    return FidelityReport(
        tuple(issue for report in reports for issue in report.issues)
    )


def as_job_issues(report: FidelityReport, *, limit: int = 500) -> list[JobIssue]:
    """Convert a report into the serializable job issue records, newest first."""
    return [
        JobIssue(
            severity=issue.severity,
            stage=issue.stage,
            source_type=issue.source_type,
            block_index=issue.block_index,
            entity_type=issue.entity_type,
            message=issue.message,
            content_preview=issue.content_preview,
        )
        for issue in reversed(report.issues)
        if issue.severity in {"warning", "error"}
    ][:limit]


def _issue(
    severity: str,
    stage: str,
    source_type: str,
    message: str,
    *,
    block_index: int | None = None,
    entity_type: str | None = None,
    preview: str | None = None,
    media_count: int | None = None,
) -> FidelityIssue:
    return FidelityIssue(
        severity=severity,
        stage=stage,
        source_type=source_type,
        block_index=block_index,
        entity_type=entity_type,
        message=message,
        content_preview=preview,
        media_count=media_count,
    )


@dataclass(frozen=True, slots=True)
class ItemRecord:
    """One meaningful source construct discovered in the article."""

    kind: str
    text: str = ""
    level: int = 0
    code: str = ""
    media_id: str | None = None
    entity_type: str | None = None
    raw_type: str | None = None
    source_index: int | None = None

    @property
    def plain(self) -> str:
        return _fold(self.text)


def _record(
    kind: str,
    *,
    text: str = "",
    level: int = 0,
    code: str = "",
    media_id: str | None = None,
    entity_type: str | None = None,
    raw_type: str | None = None,
    source_index: int | None = None,
) -> ItemRecord:
    return ItemRecord(
        kind=kind,
        text=text,
        level=level,
        code=code,
        media_id=media_id,
        entity_type=entity_type,
        raw_type=raw_type,
        source_index=source_index,
    )


def _element_text(element: Tag) -> str:
    return _fold(str(element.get_text("")))


def _media_id_of(element: Tag) -> str | None:
    for candidate in [element] + element.find_all(["img", "video"]):
        value = candidate.get("data-media-id")
        if isinstance(value, str) and value:
            return value
    return None


def _media_count_of(element: Tag, marker: str) -> int | None:
    """The renderer's item count for an unresolved-media marker, if present."""
    if marker != "unresolved-media":
        return None
    value = element.get("data-media-count")
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _marker_message(marker: str, entity_type: str | None) -> str:
    if marker == "unsupported-block":
        return (
            f"Block type {entity_type or 'unknown'} was not natively supported; "
            "text was preserved."
        )
    if marker == "unsupported-entity":
        return (
            f"Atomic entity type {entity_type or 'unknown'} was not natively supported; "
            "text was preserved."
        )
    if marker == "atomic-markdown":
        return (
            "An atomic Markdown entity was not a fenced code block; it was preserved "
            "as a code block."
        )
    if marker == "unresolved-media":
        return "Media items in the article could not be resolved to downloadable sources."
    if marker == "missing-entity":
        return "An atomic block referenced an entity absent from the entity map."
    return f"Unsupported construct: {marker}."


# ---------------------------------------------------------------------------
# Source record production
# ---------------------------------------------------------------------------


def scan_html(html: str) -> tuple[tuple[ItemRecord, ...], FidelityReport]:
    """Derive structural records from rendered article HTML plus an audit report.

    Elements tagged with ``data-fidelity`` by the renderer become explicit
    fidelity issues instead of silent drops.
    """
    issues: list[FidelityIssue] = []
    if not html or not html.strip():
        return (), FidelityReport(tuple(issues))
    soup = BeautifulSoup(html, "html.parser")
    root = soup.body or soup
    records: list[ItemRecord] = []
    for index, child in enumerate(root.children):
        if not isinstance(child, Tag):
            continue
        name = child.name.lower()
        if name in {"script", "style", "iframe", "object", "embed", "svg", "noscript"}:
            continue
        marker = child.get("data-fidelity")
        if isinstance(marker, str) and marker in _ALL_MARKERS:
            entity_type = None
            for attribute in ("data-entity-type", "data-block-type"):
                value = child.get(attribute)
                if isinstance(value, str) and value:
                    entity_type = value
                    break
            severity = "error" if marker in _ERROR_MARKERS else "warning"
            source = "block"
            if "entity" in marker:
                source = "entity"
            elif marker == "unresolved-media":
                source = "media"
            issues.append(
                _issue(
                    severity,
                    "article_html",
                    source,
                    _marker_message(marker, entity_type),
                    block_index=index,
                    entity_type=entity_type,
                    preview=_element_text(child) or None,
                    media_count=_media_count_of(child, marker),
                )
            )
        if name in {f"h{level}" for level in range(1, 7)}:
            text = _element_text(child)
            if text:
                records.append(_record("heading", text=text, level=int(name[1])))
        elif name == "p":
            text = _element_text(child)
            if text:
                records.append(_record("paragraph", text=text))
        elif name == "blockquote":
            text = _element_text(child)
            if text:
                records.append(_record("quote", text=text))
        elif name == "ol" or name == "ul":
            for item in child.find_all("li", recursive=False):
                text = _element_text(item)
                if text:
                    records.append(_record("list", text=text))
        elif name == "pre":
            records.append(_record("code", code=child.get_text("", strip=False)))
        elif name == "hr":
            records.append(_record("divider"))
        elif name == "figure":
            caption = (
                _element_text(child.find("figcaption"))
                if child.find("figcaption")
                else ""
            )
            for media in child.find_all(["img", "video"]):
                kind = "image" if media.name == "img" else "video"
                records.append(
                    _record(kind, text=caption, media_id=_media_id_of(media))
                )
        elif name == "table":
            records.append(_record("table", text=_element_text(child)))
        else:
            text = _element_text(child)
            if text:
                records.append(_record("paragraph", text=text))
    return tuple(records), FidelityReport(tuple(issues))


def _entity_lookup(content: dict) -> dict[str, dict]:
    raw = content.get("entityMap")
    if isinstance(raw, dict):
        return {str(key): value for key, value in raw.items() if isinstance(value, dict)}
    if isinstance(raw, list):
        return {
            str(item.get("key")): item["value"]
            for item in raw
            if isinstance(item, dict) and isinstance(item.get("value"), dict)
        }
    return {}


def _markdown_fence_content(value: str) -> str:
    lines = value.splitlines()
    if len(lines) >= 2 and lines[0].startswith("```") and lines[-1] == "```":
        return "\n".join(lines[1:-1])
    return value


def scan_content_state(content_state: object) -> tuple[tuple[ItemRecord, ...], FidelityReport]:
    """Inventory meaningful constructs from the raw X article content state.

    Produces structural ``source`` issues for malformed payloads and for
    relationships the renderer depends on (entity lookups, media items), so a
    silent failure upstream cannot go unnoticed.
    """
    if not isinstance(content_state, dict):
        return (), FidelityReport(
            (
                _issue(
                    "error",
                    "source",
                    "document",
                    "The article content state could not be interpreted.",
                    preview=_preview(str(content_state)) if content_state else None,
                ),
            )
        )
    blocks = content_state.get("blocks")
    if not isinstance(blocks, list):
        return (), FidelityReport(
            (
                _issue(
                    "error",
                    "source",
                    "document",
                    "The article content state has no readable blocks.",
                ),
            )
        )
    entity_map = _entity_lookup(content_state)
    native_types = extract_known_block_kinds()
    records: list[ItemRecord] = []
    issues: list[FidelityIssue] = []
    for index, block in enumerate(blocks):
        if not isinstance(block, dict):
            continue
        block_type = str(block.get("type") or "")
        text = str(block.get("text") or "")
        if block_type == "atomic":
            raw_ranges = block.get("entityRanges")
            if not isinstance(raw_ranges, list):
                issues.append(
                    _issue(
                        "error",
                        "source",
                        "entity",
                        "An atomic block has no entity ranges.",
                        block_index=index,
                    )
                )
                continue
            for item in raw_ranges:
                if not isinstance(item, dict):
                    continue
                raw_key = item.get("key")
                key = str(raw_key) if raw_key is not None else ""
                entity = entity_map.get(key)
                if not entity:
                    issues.append(
                        _issue(
                            "error",
                            "source",
                            "entity",
                            f"Entity key {key!r} referenced by an "
                            "atomic block is missing from the entity map.",
                            block_index=index,
                        )
                    )
                    continue
                entity_type = str(entity.get("type") or "").upper()
                data = entity.get("data") if isinstance(entity.get("data"), dict) else {}
                kind = entity_kind(entity_type)
                if kind == "divider":
                    records.append(
                        _record(
                            "divider",
                            raw_type="atomic:divider",
                            entity_type=entity_type,
                            source_index=index,
                        )
                    )
                elif kind == "code":
                    markdown = str(data.get("markdown") or "")
                    if markdown:
                        records.append(
                            _record(
                                "code",
                                code=_markdown_fence_content(markdown),
                                raw_type="atomic:markdown",
                                entity_type=entity_type,
                                source_index=index,
                            )
                        )
                elif kind == "media":
                    raw_items = data.get("mediaItems")
                    if not isinstance(raw_items, list):
                        issues.append(
                            _issue(
                                "error",
                                "source",
                                "media",
                                "A MEDIA entity has malformed mediaItems.",
                                block_index=index,
                                entity_type=entity_type,
                            )
                        )
                        continue
                    for media_item in extract_media_items(data):
                        media_id = media_item.get("mediaId")
                        if media_id is None:
                            issues.append(
                                _issue(
                                    "error",
                                    "source",
                                    "media",
                                    "A MEDIA item has no mediaId.",
                                    block_index=index,
                                    entity_type=entity_type,
                                )
                            )
                            continue
                        records.append(
                            _record(
                                "media",
                                media_id=str(media_id),
                                raw_type="atomic:media",
                                entity_type=entity_type,
                                source_index=index,
                            )
                        )
                elif kind == "paragraph":
                    records.append(
                        _record(
                            "paragraph",
                            text=str(data.get("formula") or text),
                            raw_type="atomic:latex",
                            entity_type=entity_type,
                            source_index=index,
                        )
                    )
                else:
                    preview = entity_preview(data, text)
                    if preview:
                        if "\n" in preview:
                            records.append(
                                _record(
                                    "code",
                                    code=preview,
                                    raw_type=f"atomic:{entity_type or 'unknown'}",
                                    entity_type=entity_type,
                                    source_index=index,
                                )
                            )
                        else:
                            records.append(
                                _record(
                                    "paragraph",
                                    text=preview,
                                    raw_type=f"atomic:{entity_type or 'unknown'}",
                                    entity_type=entity_type,
                                    source_index=index,
                                )
                            )
            continue
        kind = native_types.get(block_type)
        if kind is None:
            if text:
                records.append(
                    _record(
                        "paragraph",
                        text=text,
                        raw_type=block_type,
                        source_index=index,
                    )
                )
            continue
        if kind == "heading":
            try:
                level = header_level(block_type)
            except ValueError:
                level = 1
            records.append(
                _record(
                    "heading",
                    text=text,
                    level=level,
                    raw_type=block_type,
                    source_index=index,
                )
            )
        elif kind == "code":
            records.append(
                _record("code", code=text, raw_type=block_type, source_index=index)
            )
        elif kind == "divider":
            records.append(_record("divider", raw_type=block_type, source_index=index))
        elif text:
            records.append(
                _record(kind, text=text, raw_type=block_type, source_index=index)
            )
    return tuple(records), FidelityReport(tuple(issues))


def _media_id_map(media_entities: object) -> set[str]:
    if isinstance(media_entities, dict):
        raw = list(media_entities.values())
    elif isinstance(media_entities, list):
        raw = media_entities
    else:
        raw = []
    return {
        str(item["media_id"])
        for item in raw
        if isinstance(item, dict) and item.get("media_id") is not None
    }


def _native_raw_type(raw_type: str | None) -> bool:
    if not raw_type:
        return True
    if raw_type in extract_known_block_kinds():
        return True
    return raw_type.startswith("atomic:")


def _rectify_missing_entities(
    raw_report: FidelityReport, audit: FidelityReport
) -> FidelityReport:
    flagged = any(
        issue.source_type == "entity" and "absent" in issue.message
        for issue in audit.issues
    )
    if not flagged:
        return raw_report
    return FidelityReport(
        tuple(
            issue
            for issue in raw_report.issues
            if not (issue.source_type == "entity" and "missing" in issue.message)
        )
    )


def compare_source_html(
    raw_records: Sequence[ItemRecord],
    html_records: Sequence[ItemRecord],
    *,
    media_entities: object,
    included_source_ids: set[str],
    audit: FidelityReport,
) -> FidelityReport:
    """Verify the rendered HTML represents everything meaningful in the source.

    The rendered side is produced by the article renderer; this stage holds it
    accountable to its own input.  All text comparisons are order-sensitive, so
    silent reorderings and single-item substitutions are caught instead of
    sliding past count checks.
    """
    issues: list[FidelityIssue] = []
    known_ids = _media_id_map(media_entities) | included_source_ids
    counts = {
        kind: (_count(raw_records, kind), _count(html_records, kind))
        for kind in ("heading", "paragraph", "quote", "list", "code", "divider")
    }

    def values_of(kind: str) -> tuple[list[str], list[str]]:
        raw_values = [
            record.plain if kind != "code" else _fold(record.code)
            for record in raw_records
            if record.kind == kind
        ]
        html_values = [
            record.plain if kind != "code" else _fold(record.code)
            for record in html_records
            if record.kind == kind
        ]
        return raw_values, html_values

    for kind in ("heading", "paragraph", "quote", "list", "code", "divider"):
        source, rendered = counts[kind]
        if source != rendered:
            raw_values, html_values = values_of(kind)
            missing = next(
                (value for value in raw_values if value not in html_values),
                None,
            )
            message = (
                f"{source} {kind}(s) in the article source but "
                f"{rendered} in the rendered article."
            )
            if kind == "paragraph" and source > rendered:
                raw_type = next(
                    (
                        record.raw_type
                        for record in raw_records
                        if record.kind == "paragraph"
                        and not _native_raw_type(record.raw_type)
                    ),
                    None,
                )
                if raw_type:
                    message += f" (raw type {raw_type})"
            issues.append(
                _issue(
                    "error",
                    "source→html",
                    "block",
                    message,
                    block_index=next(
                        (record.source_index for record in raw_records if record.kind == kind),
                        None,
                    ),
                    preview=_preview(missing) if missing is not None else None,
                )
            )

    def ordered(kind: str, label: str, *, emit_missing: bool) -> None:
        raw_values, html_values = values_of(kind)
        raw_levels = [record.level for record in raw_records if record.kind == kind]
        html_levels = [record.level for record in html_records if record.kind == kind]
        raw_indexes = [record.source_index for record in raw_records if record.kind == kind]

        def mismatch(value: str) -> None:
            if value in html_values:
                issues.append(
                    _issue(
                        "error",
                        "source→html",
                        "block",
                        f"A {label} appears in a different position than in the article source.",
                        preview=_preview(value),
                    )
                )
            elif emit_missing:
                issues.append(
                    _issue(
                        "error",
                        "source→html",
                        "block",
                        f"A {label} from the article source is missing from the rendered article.",
                        preview=_preview(value),
                    )
                )

        i = j = 0
        while i < len(raw_values) and j < len(html_values):
            if raw_values[i] == html_values[j]:
                if kind == "heading" and raw_levels[i] != html_levels[j]:
                    issues.append(
                        _issue(
                            "error",
                            "source→html",
                            "block",
                            "A heading changed level between the article source and the "
                            "rendered article.",
                            block_index=raw_indexes[i],
                            preview=_preview(raw_values[i]),
                        )
                    )
                i += 1
                j += 1
                continue
            advance = next(
                (position for position in range(j + 1, len(html_values))
                 if html_values[position] == raw_values[i]),
                None,
            )
            if advance is not None:
                issues.append(
                    _issue(
                        "error",
                        "source→html",
                        "block",
                        f"A {label} appears in a different position than in the article source.",
                        preview=_preview(raw_values[i]),
                    )
                )
                i += 1
                j = advance + 1
            else:
                mismatch(raw_values[i])
                i += 1
        while i < len(raw_values):
            mismatch(raw_values[i])
            i += 1

    ordered("paragraph", "paragraph", emit_missing=counts["paragraph"][0] == counts["paragraph"][1])
    ordered("heading", "heading", emit_missing=counts["heading"][0] == counts["heading"][1])
    ordered("code", "code block", emit_missing=counts["code"][0] == counts["code"][1])

    for kind, label in (("list", "list item"), ("quote", "quote")):
        if counts[kind][0] != counts[kind][1]:
            continue
        raw_values, html_values = values_of(kind)
        source_multiset = Counter(value for value in raw_values if value)
        rendered_multiset = Counter(value for value in html_values if value)
        for text, missing in sorted((source_multiset - rendered_multiset).items()):
            for _ in range(missing):
                issues.append(
                    _issue(
                        "error",
                        "source→html",
                        "block",
                        f"A {label} from the article source is missing from the rendered article.",
                        preview=text,
                    )
                )

    media_records = [record for record in raw_records if record.kind == "media"]
    marker_counts = [
        issue.media_count
        for issue in audit.issues
        if issue.stage == "article_html" and issue.source_type == "media"
    ]
    groups: list[list[ItemRecord]] = []
    for record in media_records:
        if groups and groups[-1][0].source_index == record.source_index:
            groups[-1].append(record)
        else:
            groups.append([record])
    marker_covered: set[str] = set()
    marker_index = 0
    for group in groups:
        unresolved = [
            record for record in group if record.media_id not in known_ids
        ]
        if not unresolved:
            continue
        if (
            len(unresolved) == len(group)
            and marker_index < len(marker_counts)
            and marker_counts[marker_index] == len(group)
        ):
            marker_covered.update(record.media_id for record in group)
            marker_index += 1
    for record in media_records:
        if record.media_id in known_ids or record.media_id in marker_covered:
            continue
        issues.append(
            _issue(
                "error",
                "source→html",
                "media",
                f"Media item {record.media_id!r} in the article could not be resolved "
                "to a downloadable source.",
                block_index=record.source_index,
            )
        )
    known_media = [record for record in media_records if record.media_id in known_ids]
    if known_media:
        rendered_media = _count(html_records, "image") + _count(html_records, "video")
        if rendered_media < len(known_media):
            issues.append(
                _issue(
                    "error",
                    "source→html",
                    "media",
                    f"{len(known_media)} article media item(s) in the source but "
                    f"{rendered_media} rendered in the article.",
                    block_index=known_media[0].source_index,
                )
            )
        source_ids = Counter(record.media_id for record in known_media)
        rendered_ids = Counter(
            record.media_id
            for record in html_records
            if record.kind in {"image", "video"} and record.media_id
        )
        for media_id, expected in sorted(source_ids.items()):
            rendered = rendered_ids.get(media_id, 0)
            if rendered < expected:
                issues.append(
                    _issue(
                        "error",
                        "source→html",
                        "media",
                        f"Media item {media_id!r} appears {expected} time(s) in the article "
                        f"source but {rendered} time(s) in the rendered article.",
                        block_index=next(
                            (
                                record.source_index
                                for record in known_media
                                if record.media_id == media_id
                            ),
                            None,
                        ),
                    )
                )

    def section_signature(record: ItemRecord) -> tuple[object, ...] | None:
        if record.kind in {"media", "image", "video", "caption", "table"}:
            return None
        if record.kind == "heading":
            return ("heading", record.level, record.plain)
        if record.kind == "code":
            return ("code", _fold(record.code))
        return (record.kind, record.plain)

    source_sections = [
        signature for record in raw_records if (signature := section_signature(record))
    ]
    rendered_sections = [
        signature for record in html_records if (signature := section_signature(record))
    ]
    i = j = 0
    while i < len(source_sections) and j < len(rendered_sections):
        if source_sections[i] == rendered_sections[j]:
            i += 1
            j += 1
            continue
        advance = next(
            (position for position in range(j + 1, len(rendered_sections))
             if rendered_sections[position] == source_sections[i]),
            None,
        )
        if advance is not None:
            issues.append(
                _issue(
                    "error",
                    "source→html",
                    "block",
                    "Article content appears in a different order in the rendered article "
                    "than in the article source.",
                )
            )
            i += 1
            j = advance + 1
        else:
            i += 1
    return FidelityReport(tuple(issues))


def records_from_blocks(blocks: Sequence[Block]) -> tuple[ItemRecord, ...]:
    """Convert canonical document blocks back into comparable records."""
    records: list[ItemRecord] = []

    def inline_text(nodes: Sequence[object]) -> str:
        parts: list[str] = []
        for node in nodes:
            if isinstance(node, Text):
                parts.append(node.value)
            elif hasattr(node, "children"):
                parts.append(inline_text(tuple(node.children)))
        return _fold("".join(parts))

    for block in blocks:
        if block.kind == "heading":
            records.append(
                _record("heading", text=inline_text(block.content), level=block.level)
            )
        elif block.kind == "paragraph":
            records.append(_record("paragraph", text=inline_text(block.content)))
        elif block.kind == "quote":
            records.append(_record("quote", text=inline_text(block.content)))
        elif block.kind == "list":
            for item in block.items:
                records.append(_record("list", text=inline_text(item)))
        elif block.kind == "code":
            records.append(_record("code", code=block.code))
        elif block.kind == "divider":
            records.append(_record("divider"))
        elif block.kind in {"image", "video"} and block.media:
            records.append(
                _record(
                    block.kind,
                    text=inline_text(block.content),
                    media_id=block.media.attachment.source_id,
                )
            )
        elif block.kind == "caption":
            records.append(_record("caption", text=inline_text(block.content)))
    return tuple(records)


def _count(records: Sequence[ItemRecord], kind: str) -> int:
    return sum(1 for item in records if item.kind == kind)


def compare_blocks(
    html_records: Sequence[ItemRecord],
    block_records: Sequence[ItemRecord],
) -> FidelityReport:
    """Compare the source record with the canonical document record.

    Text-bearing kinds are compared as multisets (duplicate substitution
    ``A,A,B → A,B,B`` is caught), headings by ``(level, text)`` so a heading
    that changed level is treated as missing, and code blocks by content in
    order.
    """
    issues: list[FidelityIssue] = []
    for kind in ("heading", "paragraph", "quote", "list", "code", "divider"):
        source = _count(html_records, kind)
        rendered = _count(block_records, kind)
        if source != rendered:
            preview = next(
                (
                    item.plain
                    or _preview(item.code)
                    for item in html_records
                    if item.kind == kind and (item.plain or item.code)
                ),
                None,
            )
            issues.append(
                _issue(
                    "error",
                    "document",
                    "block",
                    f"{source} {kind}(s) in the article but {rendered} in the document model.",
                    preview=preview,
                )
            )

    def values(
        records: Sequence[ItemRecord], kind: str
    ) -> list[str] | list[tuple[int, str]]:
        if kind == "heading":
            return [
                (record.level, record.plain)
                for record in records
                if record.kind == kind and record.plain
            ]
        return [
            record.plain for record in records if record.kind == kind and record.plain
        ]

    for kind, label in (
        ("paragraph", "paragraph"),
        ("quote", "quote"),
        ("list", "list item"),
        ("heading", "heading"),
    ):
        source_values = values(html_records, kind)
        block_values = values(block_records, kind)
        for value, missing in sorted(
            (Counter(source_values) - Counter(block_values)).items()
        ):
            preview = value[1] if isinstance(value, tuple) else value
            for _ in range(missing):
                issues.append(
                    _issue(
                        "error",
                        "document",
                        "block",
                        f"A source {label} is missing from the document model.",
                        preview=_preview(preview),
                    )
                )

    source_codes = [record.code for record in html_records if record.kind == "code"]
    block_codes = [record.code for record in block_records if record.kind == "code"]
    if len(source_codes) == len(block_codes):
        i = j = 0
        while i < len(source_codes) and j < len(block_codes):
            if _fold(source_codes[i]) == _fold(block_codes[j]):
                i += 1
                j += 1
                continue
            advance = next(
                (
                    position
                    for position in range(j + 1, len(block_codes))
                    if _fold(block_codes[position]) == _fold(source_codes[i])
                ),
                None,
            )
            if advance is not None:
                issues.append(
                    _issue(
                        "error",
                        "document",
                        "block",
                        "A code block appears in a different position in the document model.",
                    )
                )
                i += 1
                j = advance + 1
            else:
                issues.append(
                    _issue(
                        "error",
                        "document",
                        "block",
                        "A code block from the article is missing from the document model.",
                        preview=_preview(source_codes[i]),
                    )
                )
                i += 1
    return FidelityReport(tuple(issues))


# ---------------------------------------------------------------------------
# Markdown / PDF verification
# ---------------------------------------------------------------------------


def _markdown_plain(value: str) -> str:
    value = _ESCAPE.sub(r"\1", value)
    value = _LINK_TARGET.sub(r"\1", value)
    value = _STRONG.sub(r"\1", value)
    value = _EMPHASIS.sub(r"\1", value)
    value = _INLINE_CODE.sub(r"\1", value)
    return _fold(value)


def _plain_candidate(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if _FENCE.match(stripped) or _DIVIDER_LINE.match(stripped):
        return False
    if _QUOTE_LINE.match(stripped) or _ORDERED_ITEM.match(stripped):
        return False
    if _UNORDERED_ITEM.match(stripped):
        return False
    if stripped.startswith("#") or stripped.startswith("!["):
        return False
    return not ("(<" in stripped and "media/" in stripped)


def check_markdown(
    html_records: Sequence[ItemRecord],
    included_media_ids: set[str],
    markdown: str,
    *,
    required_targets: Sequence[str] = (),
) -> FidelityReport:
    """Verify every source record is represented in the Markdown output."""
    lines = markdown.splitlines()
    issues: list[FidelityIssue] = []

    code_records = [item for item in html_records if item.kind == "code"]
    fence_indices = [
        index for index, line in enumerate(lines) if _FENCE.match(line.strip())
    ]
    fence_contents: list[str] = []
    for index in range(0, len(fence_indices) - 1, 2):
        fence_contents.append(
            "\n".join(lines[fence_indices[index] + 1 : fence_indices[index + 1]])
        )
    if len(fence_contents) != len(code_records):
        issues.append(
            _issue(
                "error",
                "markdown",
                "block",
                f"{len(code_records)} code block(s) in the article but "
                f"{len(fence_contents)} fence(s) in Markdown.",
            )
        )
    for item, content in zip(code_records, fence_contents, strict=False):
        if _fold(content.replace("` ` `", "```")) != _fold(item.code):
            issues.append(
                _issue(
                    "error",
                    "markdown",
                    "block",
                    "A code block's content differs in the Markdown output.",
                    preview=_preview(item.code),
                )
            )

    body_headings = sum(
        1
        for line in lines
        if _HEADING_LINE.match(line.strip())
    )
    source_headings = _count(html_records, "heading")
    if body_headings != source_headings + 1:
        issues.append(
            _issue(
                "error",
                "markdown",
                "block",
                f"{source_headings} heading(s) in the article but "
                f"{max(0, body_headings - 1)} in the Markdown body.",
            )
        )

    source_dividers = _count(html_records, "divider")
    divider_count = sum(1 for line in lines if _DIVIDER_LINE.match(line.strip()))
    if divider_count != source_dividers:
        issues.append(
            _issue(
                "error",
                "markdown",
                "block",
                f"{source_dividers} divider(s) in the article but {divider_count} in Markdown.",
            )
        )

    source_quotes = _count(html_records, "quote")
    quote_count = sum(1 for line in lines if _QUOTE_LINE.match(line.strip()))
    if quote_count != source_quotes:
        issues.append(
            _issue(
                "error",
                "markdown",
                "block",
                f"{source_quotes} quote(s) in the article but {quote_count} in Markdown.",
            )
        )

    source_list_items = _count(html_records, "list")
    list_item_count = sum(
        1
        for line in lines
        if _ORDERED_ITEM.match(line.strip()) or _UNORDERED_ITEM.match(line.strip())
    )
    if list_item_count != source_list_items:
        issues.append(
            _issue(
                "error",
                "markdown",
                "block",
                f"{source_list_items} list item(s) in the article but {list_item_count} in "
                "Markdown.",
            )
        )

    expected_media = sum(
        1
        for item in html_records
        if item.kind in {"image", "video"} and item.media_id in included_media_ids
    )
    if expected_media:
        media_targets = [
            target
            for target in _MEDIA_TARGET.findall(markdown)
            if not target.startswith(("http://", "https://"))
        ]
        if len(media_targets) < expected_media:
            issues.append(
                _issue(
                    "error",
                    "markdown",
                    "media",
                    f"{len(media_targets)} media reference(s) in Markdown for "
                    f"{expected_media} selected article media item(s).",
                )
            )

    if required_targets:
        non_url_targets = [
            target
            for target in _MEDIA_TARGET.findall(markdown)
            if not target.startswith(("http://", "https://"))
        ]
        missing_refs = [target for target in required_targets if target not in non_url_targets]
        if missing_refs:
            issues.append(
                _issue(
                    "error",
                    "markdown",
                    "media",
                    f"{len(missing_refs)} selected article media reference(s) could not "
                    "be found in the Markdown output.",
                    preview=missing_refs[0],
                )
            )

    plain_candidates = [
        _markdown_plain(line) for line in lines if _plain_candidate(line)
    ]
    rendered_paragraphs = Counter(plain_candidates)
    source_paragraphs = Counter(
        item.plain for item in html_records if item.kind == "paragraph" and item.plain
    )
    for text, missing in sorted((source_paragraphs - rendered_paragraphs).items()):
        for _ in range(missing):
            issues.append(
                _issue(
                    "error",
                    "markdown",
                    "block",
                    "A source paragraph is missing from the Markdown output.",
                    preview=_preview(text),
                )
            )

    rendered_headings = Counter(
        (len(match.group(1)), _markdown_plain(match.group(2)))
        for line in lines
        if (match := _HEADING_LINE.match(line.strip()))
    )
    source_headings = Counter(
        (min(6, item.level + 1), item.plain)
        for item in html_records
        if item.kind == "heading" and item.plain
    )
    for (_level, text), missing in sorted(
        (source_headings - rendered_headings).items()
    ):
        for _ in range(missing):
            issues.append(
                _issue(
                    "error",
                    "markdown",
                    "block",
                    "A source heading is missing from the Markdown output or its "
                    "level changed.",
                    preview=_preview(text),
                )
            )

    rendered_quotes = Counter(
        _markdown_plain(match.group(1))
        for line in lines
        if (match := _QUOTE_LINE.match(line.strip()))
    )
    source_quotes = Counter(
        item.plain for item in html_records if item.kind == "quote" and item.plain
    )
    for text, missing in sorted((source_quotes - rendered_quotes).items()):
        for _ in range(missing):
            issues.append(
                _issue(
                    "error",
                    "markdown",
                    "block",
                    "A source quote is missing from the Markdown output.",
                    preview=_preview(text),
                )
            )

    rendered_list_items = Counter()
    for line in lines:
        stripped = line.strip()
        match = _ORDERED_ITEM.match(stripped) or _UNORDERED_ITEM.match(stripped)
        if not match:
            continue
        item_text = match.group(2) if match.re is _ORDERED_ITEM else match.group(1)
        rendered_list_items[_markdown_plain(item_text)] += 1
    source_list_items = Counter(
        item.plain for item in html_records if item.kind == "list" and item.plain
    )
    for text, missing in sorted((source_list_items - rendered_list_items).items()):
        for _ in range(missing):
            issues.append(
                _issue(
                    "error",
                    "markdown",
                    "block",
                    "A source list item is missing from the Markdown output.",
                    preview=_preview(text),
                )
            )
    return FidelityReport(tuple(issues))


def _pdf_text(pdf_bytes: bytes) -> str:
    reader = PdfReader(BytesIO(pdf_bytes))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _pdf_image_count(pdf_bytes: bytes) -> int:
    try:
        reader = PdfReader(BytesIO(pdf_bytes))
        return sum(len(page.images) if hasattr(page, "images") else 0 for page in reader.pages)
    except Exception:
        return 0


def check_pdf(
    html_records: Sequence[ItemRecord],
    expected_media_ids: set[str],
    pdf_bytes: bytes,
) -> FidelityReport:
    """Verify essential source content survived into the PDF output.

    Text-layer checks are inherently best-effort: paragraphs, headings,
    quotes, and list items are confirmed with warnings because line wrapping
    and font shaping can reorder or split them. Code blocks are checked for
    exact content *multiplicity* (two identical code blocks must appear twice)
    and fail as errors, matching the Markdown stage.
    """
    try:
        folded = _fold(_pdf_text(pdf_bytes))
    except Exception:
        return FidelityReport(
            (
                _issue(
                    "error",
                    "pdf",
                    "document",
                    "The rendered PDF could not be read back.",
                ),
            )
        )
    issues: list[FidelityIssue] = []
    code_counts = Counter(
        _fold(item.code)
        for item in html_records
        if item.kind == "code" and _fold(item.code)
    )
    for code, expected in sorted(code_counts.items()):
        present = folded.count(code)
        for _ in range(max(0, expected - present)):
            issues.append(
                _issue(
                    "error",
                    "pdf",
                    "block",
                    "A code block is missing from the PDF output.",
                    preview=_preview(code),
                )
            )
    for item in html_records:
        if item.kind in {"paragraph", "quote", "list"} and item.plain:
            if item.plain not in folded:
                issues.append(
                    _issue(
                        "warning",
                        "pdf",
                        "block",
                        f"A {item.kind} could not be confirmed in the PDF text layer.",
                        preview=_preview(item.plain),
                    )
                )
        elif item.kind == "heading" and item.plain and item.plain not in folded:
            issues.append(
                _issue(
                    "warning",
                    "pdf",
                    "block",
                    "A heading could not be confirmed in the PDF text layer.",
                    preview=_preview(item.plain),
                )
            )
    expected_media = sum(
        1
        for item in html_records
        if item.kind in {"image", "video"} and item.media_id in expected_media_ids
    )
    if expected_media:
        videos = sum(
            1
            for item in html_records
            if item.kind == "video" and item.media_id in expected_media_ids
        )
        embedded_images = _pdf_image_count(pdf_bytes)
        if embedded_images < expected_media - videos:
            issues.append(
                _issue(
                    "error",
                    "pdf",
                    "media",
                    f"{expected_media - videos} article image(s) expected but {embedded_images} "
                    "embedded in the PDF.",
                )
            )
        video_labels = folded.count("Video:")
        if video_labels < videos:
            issues.append(
                _issue(
                    "error",
                    "pdf",
                    "media",
                    f"{videos} article video(s) expected but {video_labels} in the PDF.",
                )
            )
    return FidelityReport(tuple(issues))


# ---------------------------------------------------------------------------
# Entry point used by the export queue
# ---------------------------------------------------------------------------


def evaluate_export(
    analysis: Analysis,
    media_paths: dict[str, Path] | None,
    markdown: bytes | None,
    pdf: bytes | None,
) -> FidelityReport:
    """Produce the full fidelity report for an article document export."""
    article = analysis.article
    if not article:
        return FidelityReport()
    tracked = article.content_state is not None
    raw_records: tuple[ItemRecord, ...] = ()
    raw_report = FidelityReport()
    if tracked:
        raw_records, raw_report = scan_content_state(article.content_state)
    html_records, audit = scan_html(article.html)
    reports: list[FidelityReport] = [audit]
    if tracked:
        raw_report = _rectify_missing_entities(raw_report, audit)
        if raw_report.issues:
            reports.append(raw_report)

    media: list[DocumentMedia] = []
    included_source_ids: set[str] = set()
    if media_paths:
        media = list(_analysis_media(analysis, media_paths))
        included_source_ids = {
            item.attachment.source_id
            for item in media
            if item.attachment.source_id is not None
        }

    if tracked:
        reports.append(
            compare_source_html(
                raw_records,
                html_records,
                media_entities=article.media_entities,
                included_source_ids=included_source_ids,
                audit=audit,
            )
        )

    _title, blocks, _remaining = document_blocks(
        analysis.post, media, article=article
    )
    block_records = records_from_blocks(blocks)
    reports.append(compare_blocks(html_records, block_records))

    if markdown is not None:
        try:
            markdown_text = markdown.decode("utf-8")
        except UnicodeDecodeError:
            reports.append(
                FidelityReport(
                    (
                        _issue(
                            "error",
                            "markdown",
                            "document",
                            "The rendered Markdown is not valid UTF-8.",
                        ),
                    )
                )
            )
            markdown_text = ""
        raw_item_ids = {
            record.media_id
            for record in raw_records
            if record.kind == "media" and record.media_id
        }
        required_targets = tuple(
            _markdown_media_target(item)
            for item in media
            if item.attachment.source_id in raw_item_ids
        )
        reports.append(
            check_markdown(
                html_records,
                included_source_ids,
                markdown_text,
                required_targets=required_targets,
            )
        )
    if pdf is not None:
        reports.append(check_pdf(html_records, included_source_ids, pdf))
    return _merge(*reports)