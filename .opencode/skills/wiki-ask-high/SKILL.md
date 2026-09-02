---
name: wiki-ask-high
description: Answer a question from this local knowledge base at the High retrieval tier — an autonomous, self-planned loop over all three tools (retrieve, graph_neighbors, knowledge_tree) for up to five rounds, with duplicate-call detection to avoid spinning. Use for hard, open-ended, or multi-hop questions that need iterative evidence gathering, or when a benchmark explicitly selects the High tier.
metadata:
  audience: wiki-users
  retrieval-tier: high
---

# Wiki Ask — High tier

Answer using only the `mobile-retrieval` MCP server. This tier plans its own retrieval loop over all three tools, up to five rounds, stopping as soon as the evidence is sufficient.

If the message is a greeting, UI/help request, or otherwise does not ask about the local knowledge base, do not retrieve; answer it normally and ignore the planning prerequisite below.

## Required prerequisite

`wiki-retrieval-planner` is injected immediately before this Skill by the Mobilework plugin. Never call the `skill` tool to load or replace it. Start from its plan, then adapt the remaining evidence gaps after each result. This Skill, not the planner, owns tool execution and the final answer.

## Available tools

- `mobile-retrieval_retrieve(query, scope="wiki"|"source"|"both", channels=["vector","keyword","graph"], top_k=10, rrf_k=60, include_content=false, verbose=false)` — hybrid recall; graph channel auto-disabled for `scope="source"`. Empty results is a valid "no hit", not an error. Note: the vector index covers wiki pages only, so `scope="source"` recall is keyword-driven — prefer specific terms/names in the query when searching sources.
- `mobile-retrieval_graph_neighbors(seed, depth=1, top_k=10)` — one-hop (or `depth`-hop) neighbours of a page for relationship and multi-hop questions.
- `mobile-retrieval_knowledge_tree()` — deterministic directory of the knowledge base (totals, categories, page IDs); needs no query. Use it for structure/inventory questions or to discover seeds.

## Loop policy

1. **Start from the plan.** Use the planner's sub-goals and first route. Start with `knowledge_tree` only when the plan needs structure or a seed that is not yet known.
2. **Act, then reflect.** After each call, inspect all returned content, snippets, IDs, and relationships. Update which sub-goals are now supported. Choose the next tool to close the largest remaining gap — e.g. `retrieve(scope="source")` for provenance, `graph_neighbors` to follow a relationship, `retrieve(scope="both")` to widen coverage.
3. **Escalate deliberately.** Set `include_content=true` when you need prose; raise `top_k` or switch `scope` when coverage is thin. Set `verbose=true` only for channel-ablation or latency questions.
4. **Detect duplicates.** The server flags a repeated `(scope, channels, normalized query)` call with `duplicate:true` and a `previous` pointer. If you see it, you are spinning — change the tool, scope, seed, or query substance, or stop and answer. Never repeat a call for cosmetic reasons.
5. **Stop.** End as soon as the evidence answers the question, or after five rounds, whichever comes first.

## Answer from evidence

- Answer the original question, synthesizing the evidence and citing sources with their human-readable document titles. Never expose a path or internal page/source/chunk/claim ID in ordinary user-facing prose.
- Treat every returned `path` as a citation identifier, never a workspace file — no `read`, grep, or shell on returned paths. Obtain prose through `include_content=true`.
- Separate retrieved facts from inference and label inference explicitly. Never invent pages, IDs, relationships, or sources.
- Pages derived from the same original document are one source, not independent corroboration. Describe them as multiple passages from the same source.
- If five rounds do not fully answer, present what is supported, name the exact evidence gap, and note whether it is a corpus gap or a retrieval limit. Mention `embedding.status` degradation only when it affects confidence.

## User-facing boundary

Keep planning, route selection, evidence-gap updates, and duplicate detection internal. Do not mention the tier, Planner, Skill, MCP server, tool names, channels, scope, parameters, call count, or round numbers. Do not announce an “internal plan”; begin retrieval directly. Answer only what the user asked in natural language. Technical traces are allowed only when the user explicitly asks to debug retrieval.
