# Phase 1 Hardening — Step 1: State Verification and Blind-Spot Analysis

**Date:** 2026-08-10
**Repo:** `/home/soloarch/ai-t00lz/crXte`
**Branch:** `feature/export-fidelity-validation`
**Input:** the independent-review hardening plan (708 lines, 17 sections) that originated the Phase 1 hardening pass; its findings and execution record live in this document and in `docs/export-fidelity.md`.

---

## Summary

This milestone executed the plan's **Section 1 (verify current local state)** and performed the **Section 2 validity investigation** of the central architectural concern (the raw-source blind spot). No code changes were made in this step — execution of the full hardening plan (Sections 2–17) was approved by the user and is the next phase.

## 1. Local state verification (Section 1 of the plan)

All five reported Phase 1 commits are present on `feature/export-fidelity-validation`:

```
0e80c2a fix: exclude non-embedded media from fidelity audit
442e08e test: lock literal angle-bracket export behavior
2dbe849 feat: surface export fidelity issues in job status
7446c36 chore: ignore stray source archive and capture files
6c1e919 feat: verify export fidelity of Markdown and PDF outputs
```

- Branch head: `0e80c2a` (no upstream remote branch yet)
- `main` @ `2afe2a9` (`fix: preserve X article Markdown entities`), tracking `origin/main`
- `git diff origin/main...HEAD`: **12 files changed, +1648 / −34**
- Plan file itself is the only untracked file in the working tree
- No working-tree modifications to Phase 1 code

**Verdict:** the five claimed commits and all intended changes are genuinely present. History preserved; no reset or rewrite performed.

## 2. Central architectural concern: is the raw-source blind spot valid? (Section 2 investigation)

Traced the real implementation: `extractors.analyze_url` → `article_html.to_html(content_state)` → `analysis.article.html` → `fidelity.scan_html(html)` → `document_blocks` → Markdown/PDF.

### Previously covered (renderer markers exist)

- Unknown block type → `data-fidelity="unsupported-block"` (`article_html.py:400-404`)
- Unknown atomic entity type → `data-fidelity="unsupported-entity"` (`article_html.py:362-365`)
- Atomic block referencing an entity key absent from the entity map → `data-fidelity="missing-entity"` (`article_html.py:310`, `:317`)
- Non-fenced atomic MARKDOWN entity → `data-fidelity="atomic-markdown"` (`article_html.py:336-341`)
- MEDIA entity with items but no resolvable sources → `data-fidelity="unresolved-media"` (`article_html.py:357-361`)

### Valid blind spots found (content can vanish with NO marker)

1. **Whole-article silent drop** (`article_html.to_html`, lines 408-416): if `article` is not a dict, `content_state` is not a dict, or `blocks` is not a list, `to_html` returns `[]` with no marker. A malformed or future-restructured X payload produces an empty render, `scan_html("")` yields zero records, `compare_blocks` compares empty-vs-empty, and the export is reported **"fully complete" with zero content and zero issues**.
2. **MEDIA entity inner drops** (`_render_atomic`, lines 343-349): a MEDIA entity whose `mediaItems` is missing/not a list, or an item lacking `mediaId`, is silently `continue`-d — no `data-fidelity` marker is emitted for the dropped item.

**Verdict:** the concern is **VALID**. The fidelity inventory currently begins at rendered HTML, so the `content_state → rendered HTML` boundary is unmonitored; "silent content loss is impossible" cannot be claimed until a raw-source inventory exists and is compared against the rendered inventory.

## 3. Recommended resolution (per plan, pending execution)

- Add a raw `content_state` scanner (e.g. `scan_content_state`) producing an independent source inventory: block index/order, block type, meaningful text, entity keys referenced, entity types, MARKDOWN/code payload identity, media identity, dividers/LaTeX, unknown block/entity types, missing entity references.
- Compare raw inventory vs rendered inventory (`scan_html`) so every meaningful raw construct either maps to a rendered representation or produces an explicit fidelity issue.
- Do NOT duplicate the renderer; the scanner only identifies constructs.
- Follow TDD: failing regressions first (unknown raw block, unknown atomic entity, missing entity relationship, known-source-item-intentionally-removed).
- Preserve: media-selection semantics (`include_document_media=False` must not produce errors), the 13-code-block MARKDOWN regression for `https://x.com/RohOnChain/status/2080296261576687751`, the multi-MARKDOWN ordering test, and security guarantees for any new preview/fallback rendering.

## 4. Additional review items queued (Sections 4–14)

- `compare_blocks` multiplicity/ordering (duplicate substitution `A,A,B → A,B,B`, reordered code blocks, duplicate identical paragraphs)
- Severity semantics (warning when preserved-but-degraded vs error when meaningful content absent) and "complete" phase truthfulness
- `_MEDIA_TARGET` audit: relative Markdown links must not count as media
- Stage attribution of discrepancies (`source→html`, `html→document`, `document→markdown/pdf`) without duplicating one root cause
- Security re-audit incl. safely rendered UI previews of unsupported content
- Real RohOnChain export smoke (fresh instance, inspect `fidelity_issues`, 13 code sections, ordering)
- Architecture document `docs/export-fidelity.md` (guarantees matching the implementation)
- Final verification (`uv sync --locked --dev`, ruff, full pytest count, compileall, `git diff --check`, clean tree)
- Commit plan (keep the five commits; add logical new commits such as `test: expose raw article fidelity blind spots`, `feat: inventory raw article source structures`)
- Then: push branch, open PR against `main` titled `feat: verify export fidelity end to end`, do NOT merge

## 5. Decision

User approved **full execution of the plan** in this session. Next messages continue with the TDD hardening work (Section 2–17).