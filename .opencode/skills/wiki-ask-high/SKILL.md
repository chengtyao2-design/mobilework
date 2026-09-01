---
name: wiki-ask-high
description: Answer a question from this local knowledge base at the High retrieval tier — an autonomous, self-planned loop over all three tools (retrieve, graph_neighbors, knowledge_tree) for up to five rounds, with duplicate-call detection to avoid spinning. Use for hard, open-ended, or multi-hop questions that need iterative evidence gathering, or when a benchmark explicitly selects the High tier.
metadata:
  audience: wiki-users
  retrieval-tier: high
---

# Wiki Ask — High tier

Answer using only the `mobile-retrieval` MCP server. This tier plans its own retrieval loop over all three tools, up to five rounds, stopping as soon as the evidence is sufficient.

## Available tools

- `mobile-retrieval_retrieve(query, scope="wiki"|"source"|"both", channels=["vector","keyword","graph"], top_k=10, rrf_k=60, include_content=false, verbose=false)` — hybrid recall; graph channel auto-disabled for `scope="source"`. Empty results is a valid "no hit", not an error. Note: the vector index covers wiki pages only, so `scope="source"` recall is keyword-driven — prefer specific terms/names in the query when searching sources.
- `mobile-retrieval_graph_neighbors(seed, depth=1, top_k=10)` — one-hop (or `depth`-hop) neighbours of a page for relationship and multi-hop questions.
- `mobile-retrieval_knowledge_tree()` — deterministic directory of the knowledge base (totals, categories, page IDs); needs no query. Use it for structure/inventory questions or to discover seeds.

## Loop policy

1. **Plan.** Decompose the question into sub-goals and pick the tool that most directly serves the first gap. Start with `knowledge_tree` only when you need structure or a seed you do not yet have.
2. **Act, then reflect.** After each call, inspect all returned content, snippets, IDs, and relationships. Update which sub-goals are now supported. Choose the next tool to close the largest remaining gap — e.g. `retrieve(scope="source")` for provenance, `graph_neighbors` to follow a relationship, `retrieve(scope="both")` to widen coverage.
3. **Escalate deliberately.** Set `include_content=true` when you need prose; raise `top_k` or switch `scope` when coverage is thin. Set `verbose=true` only for channel-ablation or latency questions.
4. **Detect duplicates.** The server flags a repeated `(scope, channels, normalized query)` call with `duplicate:true` and a `previous` pointer. If you see it, you are spinning — change the tool, scope, seed, or query substance, or stop and answer. Never repeat a call for cosmetic reasons.
5. **Stop.** End as soon as the evidence answers the question, or after five rounds, whichever comes first.

## Answer from evidence

- Answer the original question, synthesizing across the tools used and citing each supporting `path` with `page_id`/`source_id`/`chunk_id` when present.
- Treat every returned `path` as a citation identifier, never a workspace file — no `read`, grep, or shell on returned paths. Obtain prose through `include_content=true`.
- Separate retrieved facts from inference and label inference explicitly. Never invent pages, IDs, relationships, or sources.
- If five rounds do not fully answer, present what is supported, name the exact evidence gap, and note whether it is a corpus gap or a retrieval limit. Mention `embedding.status` degradation only when it affects confidence.
