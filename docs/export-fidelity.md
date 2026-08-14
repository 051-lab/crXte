# Export Fidelity Architecture

crXte guarantees that a supported X Article is exported without silent content
loss. Every meaningful construct in the original article must either map to a
rendered/canonical representation or produce an explicit fidelity issue.

## Pipeline and stages

```
content_state ── article_html.to_html ──▶ source HTML
source HTML ───── documents._article_blocks ─▶ canonical Blocks
canonical Blocks ────────────────────────▶ Markdown and PDF
```

Each boundary is measured and issues are attributed to a stage:

| Stage | Meaning |
|---|---|
| `source` | Structural problems in the raw X payload itself |
| `source→html` | Raw source constructs that the renderer did not represent |
| `article_html` | Renderer markers (`data-fidelity`) for constructs it could not map natively |
| `document` | Canonical Blocks that lost source content |
| `markdown` | Markdown output missing source content |
| `pdf` | PDF output missing source content |

Severity contract:

* `info` — internal observation, no user impact (not surfaced)
* `warning` — content preserved, but structure or presentation changed
* `error` — supported source content was not represented at all

An export counts as fully complete only when its report has no `error` issues.
A job whose outputs finished with content-loss errors is labeled
`Finished with content issues` in its phase, never `Complete`.

## Raw-source boundary

The single largest blind spot was the raw X `content_state`: malformed or
future-restructured payloads could make `article_html.to_html()` return an
empty render with no marker, so an empty export passed as complete. Closed by
auditing the renderer's *input* in addition to its *output*:

1. **Capture** — `gallery_runner` records the raw article dict (with
   `content_state` and `media_entities`) that gallery-dl passes to the
   renderer, into a per-analysis temp file (`CR_XTE_RAW_CAPTURE`).
2. **Inventory** — `fidelity.scan_content_state` produces an item inventory
   from the raw blocks and entities, mirroring the renderer's own
   classification (`extract_extractors`) plus structural diagnostics:
   malformed payloads, missing `blocks`, entity references absent from the
   entity map, MEDIA entities with malformed/empty `mediaItems`, and items
   without `mediaId`.
3. **Comparison** — `compare_source_html` holds the rendered HTML accountable
   to the raw inventory: counts per construct kind, order-sensitive text
   comparison, media resolution checks.

The invariant enforced at this boundary:

> Every meaningful raw source construct must either map to a
> rendered/canonical representation or produce an explicit fidelity issue.

Exact behavior:

* Unknown block types with meaningful text degrade to a paragraph guess; if
  the renderer dropped them, counts differ and an error names the raw type.
* Unknown atomic entity types behave the same via their preserved preview
  text. When the renderer kept the text behind a `data-fidelity` marker, the
  marker's warning applies and no error is raised.
* A MEDIA item is only "resolved" when its `mediaId` is present in the
  article `media_entities` map (or the export's included sources). Resolution
  is audited **per item**: each raw media record must be resolved, or covered
  by an `unresolved-media` marker from *its own* MEDIA entity, or it produces
  an error naming the specific `mediaId`. A mixed entity where one item
  resolves and another does not is therefore reported, not masked by the
  successful item.
* `unresolved-media` markers are attributed to their originating MEDIA
  entity. The source inventory carries each raw MEDIA record's Draft/X entity
  key (`ItemRecord.entity_key`), and the renderer emits the same key on the
  marker (`data-entity-key`, attribute-escaped). Attribution is entity-local:
  a keyed marker is matched to its own raw MEDIA entity, and the raw entities
  are grouped per entity key, so two MEDIA entities referenced from the same
  atomic block remain distinct. Markers without an entity key (legacy or
  hand-written HTML) fall back to structural pairing in order.
  **Consume-on-consider:** a marker that is considered for a MEDIA construct
  is consumed by that construct even when its `data-media-count` disagrees
  with the entity's item count — it can never migrate forward to a later
  entity. A marker only covers an entity when its count matches the entity's
  item count and the whole entity is unresolved; otherwise the entity's items
  are conservatively reported.
* Rendered media identity is verified by `data-media-id`: every known source
  media item must appear with the correct `mediaId` in the rendered figures.
  Substituting one item's identity for another (source `A,B` rendered as
  `A,A`) is an error naming the short item.
* `missing-entity` is reported once: the renderer's marker takes precedence
  when present, otherwise the raw-stage issue stands.
* MEDIA items do not participate in text counts; their survival is verified
  through per-item resolution, rendered identity, the Markdown media
  references, and the PDF media audit.

## Multiplicity and ordering

Count checks were insufficient: `source A,A,B → output A,B,B` passes counts
and set checks. Every comparison stage is therefore multiset- and
order-sensitive:

* paragraphs, headings, and code blocks are compared as ordered sequences
  across the whole article (`source→html` and the document stage); a shifted
  item produces a *position* error, an absent item a *missing* error (whose
  preview names the first genuinely missing text);
* list items and quotes are compared as multisets when counts match, so a
  duplicated item substituted for a missing sibling is caught;
* paragraphs, quotes, list items, and headings are compared as multisets in
  the Markdown stage; duplicated source content must appear with the same
  multiplicity in the output;
* heading levels are part of the identity: a `header-two` demoted to `<h1>`
  in the render, or to a single `#` in Markdown, is an error. Markdown
  headings are expected one level below the source level (the document title
  is the master `#`);
* a cross-kind reordering (a heading swapping with a following paragraph) is
  detected against the article's structural sequence, excluding media and
  metadata records that have no stable cross-kind identity;
* duplicate legitimate content (`A,B,A → A,B,A`) passes cleanly at every
  stage;
* reordered Markdown code entities are detected at the raw boundary as well
  as in the Markdown stage.
* When a kind's counts already differ, a single count error (with a missing
  item preview) replaces per-item missing errors, so one root cause does not
  produce a pile of redundant issues.

## Media reference verification

`check_markdown` receives the *known* document-media mappings produced by the
renderer pipeline (`_markdown_media_target`). Media verification proceeds in
two layers:

1. Legacy compatibility count: the number of non-absolute link targets in
   the Markdown output must reach the number of included article media
   records. This layer is intentionally broad — it counts any
   non-http(s)/https target, including ordinary relative links such as
   `[Documentation](docs/readme.md)`, so it can prove presence of *some*
   reference but never *which* item was referenced.
2. Exact known-reference check (`required_targets`): every selected article
   media item's expected relative target must literally appear in the
   Markdown output. Unrelated relative links cannot satisfy this check, so
   they cannot mask a missing reference for a selected media item.

Intentional exclusion is respected: when `include_document_media` is off,
`document_media` is empty and no media item is *included*, so no Markdown/PDF
media reference is required and no media error is raised. Source existence
and requested inclusion are tracked separately: source items are always
inventoried, only *included* items are required in document outputs.

## Security

* All article text is HTML-escaped by the renderer before it becomes source
  HTML; X-controlled payloads never enter memory as raw HTML.
* Fidelity issue messages and `content_preview` are plain extracted text
  (folded element text), never markup.
* The web UI renders issue messages and previews with `textContent`, making
  stored/preview content inert even if it contains markup characters.

## Captured and known limitations

* Raw-source checks apply to analyzes produced by the current gallery-dl
  version whose renderer captured a `content_state`; older persisted analyses
  keep the previous behavior (no raw boundary).
* Media figure expectations assume the renderer is deterministic (it is), and
  per-kind availability is resolved through `media_entities`; attachments
  whose source metadata diverges from `media_entities` are treated as
  unresolved and surfaced. Entity-key attribution requires the marker to
  carry the source entity key; markers produced before that attribute existed
  (or any marker lacking it) fall back to structural order pairing with
  consume-on-consider semantics, which stays conservative but cannot prove
  entity identity as strongly.
* The Markdown body-heading check assumes a single H1 document title; a
  future multi-title layout would need that assumption revisited.
* PDF text-layer checks are best-effort for paragraphs/headings (warnings);
  code blocks are checked for exact content multiplicity and media counts
  are exact errors.

## Verification

Regression suite: `tests/test_fidelity_raw_source.py` (raw-source boundary,
including the 13-fence article shape, per-item media identity, entity-local
marker attribution, marker migration, and same-block multi-entity regressions),
`tests/test_fidelity.py` (document stage, markdown/pdf, media targets),
`tests/test_extractors.py` (heading level mapping), `tests/test_queue.py`
(phase semantics, media exclusion). The full suite is 166 tests. Live
reference article used in manual validation:
`https://x.com/RohOnChain/status/2080296261576687751` (13 code sections).