# Upstream provenance

- Source repository: `https://github.com/chengtyao2-design/llm_wiki.git`
- Source commit inspected during extraction: `a7b542e`
- Upstream license: GNU General Public License v3
- Primary source files: `src-tauri/src/commands/search.rs` and `src-tauri/src/commands/vectorstore.rs`

The standalone implementation removes Tauri command wrappers and UI state. It preserves the upstream retrieval constants and behavior where practical, then adds CLI commands, deterministic fixtures, phase timing, evaluation metrics, report generation, and an indexed comparison engine.

## Provenance convention

GPLv3-derived retrieval logic is confined to the isolation zone:

- `wiki_retrieval/channels/keyword.py` — term/phrase scoring (upstream `search.rs` / `engine.rs`).
- `wiki_retrieval/channels/graph.py` — wikilink adjacency, graph quota, neighbour blending.

Every isolation-zone module must begin with this header block:

```python
# ---------------------------------------------------------------------------
# GPLv3 provenance — see third_party/wiki-retrieval-mcp/UPSTREAM.md
# Derived from github.com/chengtyao2-design/llm_wiki @ a7b542e (GPLv3):
#   src-tauri/src/commands/search.rs, .../vectorstore.rs
# This module is excluded from git until replaced by a self-owned implementation.
# ---------------------------------------------------------------------------
```

Each ported function carries a one-line marker naming the exact upstream site:

```python
# provenance: upstream search.rs:172 (GPLv3)
```

Stray markers elsewhere in `wiki_retrieval/` (e.g. `# tools.rs:10` in `textutil.py`) must be reviewed one by one: generic text handling is treated as self-owned and loses the marker; anything genuinely derived moves into the isolation zone.

While the port is in place, `wiki_retrieval/` is git-ignored and `pyproject.license` stays unset. Both change only after a self-owned scoring implementation replaces the isolation zone.

## Isolation-zone rulings (WP-E)

- `wiki_retrieval/textutil.py`: CJK bigram tokenization, stopword filtering, snippet
  windowing and `page_id` derivation are reviewed as generic text handling and treated
  as self-owned. No provenance marker is carried here.
- `wiki_retrieval/store.py`: the `1/(1+distance)` similarity conversion and per-page
  best-hit selection originate in `vectorstore.rs`, but they are a thin, conventional
  adapter over LanceDB rather than the ranking algorithm itself. They stay in `store.py`
  without a provenance marker. In any case the whole `wiki_retrieval/` tree is git-ignored,
  so no derived code leaves the isolation boundary regardless of this classification.
