---
name: wiki-maintainer
description: Maintain this project's local Obsidian-compatible LLM Wiki from raw sources. Use whenever sources are added, changed, moved, restored, or deleted; when the user asks to sync, ingest, update, clean, rebuild, or maintain the wiki; or when an automatic watcher supplies a pending batch. Preserve exact provenance, reconcile shared pages, use staging, and commit through the deterministic lifecycle engine.
metadata:
  opencode/slash: "true"
  audience: wiki-users
  workflow: lifecycle-sync
---

# Wiki Maintainer

Treat `raw/sources/` as source code and `wiki/` as a compiled artifact. The Python engine owns change detection, source identity, transactions, conflict checks, index/log maintenance, and recoverable archival. You own semantic analysis and page rewriting.

All commands run from the project root. On this Windows project use the packaged
entry point `.venv/Scripts/wiki.exe`. On POSIX systems the equivalent path is
`.venv/bin/wiki`.

## Start or resume a batch

```bash
.venv/Scripts/wiki.exe --root . pending --summary
```

If there is no pending batch, run:

```bash
.venv/Scripts/wiki.exe --root . prepare
```

Read `purpose.md`, `schema.md`, and `wiki/index.md`. Do not print or read the complete
`work-order.json` for a non-trivial batch: it embeds every source chunk body and can
overflow tool output or model context. The compact summary contains the source IDs
and paths. Process exactly one source end-to-end before inspecting the next source.
Never issue parallel or bulk source reads. Before processing that source, request
only its bounded metadata:

```bash
.venv/Scripts/wiki.exe --root . pending --source-id SOURCE_ID
```

This returns chunk IDs, hashes, ordinals, and sizes without chunk bodies or previews.
Do not request metadata for every source up front. When exact evidence is needed,
request only one bounded chunk:

```bash
.venv/Scripts/wiki.exe --root . pending --source-id SOURCE_ID --chunk ORDINAL
```

Read the event's `extracted_path` only if chunk-level access is insufficient, and
then read it sequentially in bounded sections. Finish that source's Stage 1 analysis
entry before moving to the next source. Work
only inside the `staging_wiki` path named by the summary. Never edit active `wiki/`
pages during a batch; the engine needs this boundary to detect concurrent human edits
and apply the result safely.

Use the deterministic aids in each event before broad reads:

- `diff` lists content-addressed chunks added, removed, and unchanged. For a small modification, inspect changed chunks and `affected_claims` first.
- `affected_claims` identifies existing assertions whose supporting chunks changed. Preserve unrelated claims byte-for-byte where possible.
- `identity_candidates` ranks existing pages with BM25. Check exact title/alias matches first; when an identity exists, use `checkout-page` and merge instead of creating a duplicate.

## Mandatory two-stage semantic ingest

For every `new`, `modified`, or `restored` event, use two explicit stages. Do not write or update Wiki pages until Stage 1 is complete.

### Stage 1 — analyze and cache

Read the extracted source and write the structured analysis to the work order's `analysis_path` using this root shape: `{"version":1,"batch_id":"...","sources":[...]}`. The `sources` array must contain exactly one entry per processable source with:

- `source_id` and `source_path`;
- `entities`: named people, organizations, companies, products, tools, places, or issuing bodies;
- `concepts`: reusable theories, systems, methods, metrics, or ideas;
- `procedures`: actionable workflows with enough ordered steps to become a `skills/` page;
- `arguments`: the source's material claims or conclusions;
- `existing_matches`: pages that should be updated instead of duplicated;
- `page_plan`: each proposed page path, category, title, evidence chunk IDs, and action (`create`, `merge`, `replace`, or `skip`);
- `skipped_candidates`: salient candidates not promoted, each with a short reason.

Compare candidates with `wiki/index.md`, event `identity_candidates`, and relevant existing summaries. This cached analysis is the semantic contract for Stage 2 and makes classification reviewable.

### Stage 2 — generate from the cached plan

Only after Stage 1 is saved, create or update the planned pages in `staging_wiki`, then record source-to-page lineage and commit. Every staged page must appear in `page_plan`; do not invent additional pages during generation.

Apply this ontology strictly:

- `references/` — a summary of one concrete source. Every successfully extracted new/restored source gets one; merge only when it is genuinely the same document identity.
- `entities/` — a concrete named person, organization, company, product, tool, place, or issuing body. Name the page after the entity itself. `神州高铁` is an entity; `神州高铁公司综合能力` is a report topic and must not be an entity page.
- `concepts/` — a reusable abstract theory, policy, method, metric, or management system.
- `skills/` — an executable procedure supported by ordered steps, conditions, or responsibilities in the source. Policies such as meeting arrangement, vehicle use, and accident handling should yield a skill when the source contains operational steps.
- `synthesis/` — a cross-source comparison or conclusion supported by at least two independent source IDs. Never use it for a single-source summary.

Coverage gate: before finishing, account for every salient named entity, reusable concept, and actionable procedure as `create`, `merge`, or `skip` with a reason. Do not force irrelevant page types merely to populate folders, but do not silently drop a category candidate.

## Interpret lifecycle events

- `new`: run both semantic stages, then distill the extracted source into its required reference page and all justified concept, entity, skill, or synthesis pages.
- `modified`: reread the new source and every affected staged page. If `reason` is `semantic_revision_changed`, read the full source and reclassify all existing output even when the chunk diff is empty. Replace outdated or misclassified pages; do not merely append a new summary.
- `moved`: preserve the stable `source_id`, update exact source paths, and retain page identity. Run `rewrite-moves` first, then review the result.
- `restored`: treat it as a returning source with the same identity and reconcile it with current pages.
- `deleted`: if an affected page has surviving sources, rebuild it from those surviving sources and remove claims supported only by the deleted source. If it has no surviving sources, delete its automatically copied file from `staging_wiki`; commit then moves the active page into recoverable trash. This staging deletion is required because `prepare` copies every affected page for review.

`work-order.claim_pruning` reports claim blocks already removed or source-pruned deterministically during deletion. Review remaining unmarked prose, but do not reintroduce a removed claim unless a surviving source supports it.

Loose filename matching is unsafe. Use the exact `source_id` and canonical path from the work order.

## Read sources economically

For new, modified, moved, and restored events, prefer the event's `extracted_path`. If extraction failed or the source is an image, inspect the original `path` with the available file/PDF/vision tools. For a shared page after modification or deletion, also read the surviving source inputs or originals before rewriting; removing a frontmatter entry alone would leave unsupported prose behind.

Use `summary:` and `wiki/index.md` to find existing pages before full reads. Merge into an existing identity when it represents the same concept. Create a new page only for a distinct, reusable knowledge unit.

When a new source should enrich an existing managed page that was not already copied into `staging_wiki`, check it out before editing. This records the active content hash and prevents a later commit from overwriting a concurrent human change:

```bash
.venv/Scripts/wiki.exe --root . checkout-page --batch BATCH_ID --page concepts/existing.md
```

Never copy an active page into staging by hand. User-managed pages without `wiki_managed: true` are outside this compiler's ownership.

## Page contract

Every staged page must be valid Obsidian Markdown and begin with this machine-readable shape. Keep arrays as JSON-style inline arrays so the deterministic validator can parse them without a YAML dependency.

```markdown
---
title: "Page title"
category: "concepts"
tags: ["example"]
aliases: []
wiki_managed: true
source_ids: ["src_0123456789abcdef"]
sources: ["raw/sources/example.md"]
summary: "One or two sentences, no more than 200 characters."
base_confidence: 0.50
lifecycle: "draft"
lifecycle_changed: "2026-08-17"
tier: "supporting"
created: "2026-08-17T00:00:00+00:00"
updated: "2026-08-17T00:00:00+00:00"
---
```

Use categories `sources`, `concepts`, `entities`, `skills`, `references`, or `synthesis`, stored under the same-named directory. Preserve `created` on update. Record all contributors in both `source_ids` and `sources`, in matching identity/path sets. Use `[[category/page|label]]` links. Mark non-verbatim synthesis with `^[inferred]` and unresolved conflicts with `^[ambiguous]`.

### Claim-level provenance

After each material factual paragraph, bullet, result, or conclusion, add a compact marker on the next line:

```markdown
Residual connections make optimization of deep networks easier.
<!-- wiki-claim: {"id":"clm_residual_optimization","source_ids":["src_0123456789abcdef"],"chunk_ids":["chk_0123456789abcdef"]} -->
```

- Keep the claim ID stable when revising the same assertion; use a new `clm_...` ID for a distinct assertion.
- `source_ids` must be a non-empty subset of page-level provenance. Use chunk IDs supplied by the event.
- Headings, navigation text, and the Sources section do not need markers.
- Legacy pages remain valid, but materially rewritten sections should gain markers progressively.

The engine validates and indexes these markers. On deletion it can remove an exclusively backed claim or prune one source from a shared claim without rewriting the entire page.

Do not stage `index.md` or `log.md`; the engine regenerates them. Do not alter raw sources.

## Finish the batch

For every event except `deleted`, record the complete final set of pages to which that source contributes. An empty list is valid if the source yielded no durable knowledge.

```bash
.venv/Scripts/wiki.exe --root . record-source --batch BATCH_ID --source-id SOURCE_ID --pages-json '["references/example.md", "concepts/example.md"]'
```

For a path-only move, first run:

```bash
.venv/Scripts/wiki.exe --root . rewrite-moves --batch BATCH_ID
```

Then commit:

```bash
.venv/Scripts/wiki.exe --root . commit --batch BATCH_ID
```

If validation fails, fix the staged pages and retry. If it reports an active-page conflict, stop and report the exact page; do not overwrite the user's concurrent edit. `abort` preserves the abandoned batch for diagnosis and never modifies the active wiki.

To recover archived pages for inspection, run `restore-trash --batch BATCH_ID`. It copies them into `.wiki-state/restored/<batch>/wiki` without republishing stale provenance. Re-add the raw source and sync before bringing that knowledge back into the active Wiki.

## Index rebuild after commit

A successful `commit` triggers a retrieval reindex automatically: the engine lazily calls the retrieval package to rebuild the vector index and refresh index freshness metadata. The reindex is best-effort — the vector index is a derived artifact, so a reindex failure never rolls back the commit. Read the `reindex` field in the commit output:

- `reindex.status == "ok"` — the index was rebuilt and is fresh.
- `reindex.status == "skipped"` — the retrieval package or its embedding service is unavailable; the committed wiki is still correct, but retrieval will report `stale_index` until the next successful build.
- `reindex.status == "failed"` — rebuild was attempted and failed; the error is recorded but the commit stands.

To commit without touching the index, pass `--no-reindex`. To rebuild the index out of band on Windows, run `.venv/Scripts/python.exe -m wiki_retrieval.index` (POSIX: `.venv/bin/python -m wiki_retrieval.index`). A stale index self-heals on the next successful commit or manual build.

## Report

Return a compact summary of sources added/modified/moved/restored/deleted, pages updated, pages archived, the `reindex` status, and any extraction or conflict warnings. A successful sync should not require routine user decisions.
