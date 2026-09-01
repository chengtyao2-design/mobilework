---
name: wiki-ask-medium
description: Answer a question from this local knowledge base at the Medium retrieval tier — decompose the question into verifiable information points, run three-channel recall (vector + keyword + graph) per point, judge whether the evidence is sufficient, and make at most two rounds of supplementary retrieval. Use for multi-part or explanatory questions that need corroborated evidence, or when a benchmark explicitly selects the Medium tier.
metadata:
  audience: wiki-users
  retrieval-tier: medium
---

# Wiki Ask — Medium tier

Answer using only the `mobile-retrieval` MCP server. This tier decomposes the question and gathers corroborated evidence over at most two retrieval rounds.

## Procedure

1. **Decompose.** Restate the question as a short list of verifiable information points (the distinct facts, definitions, or relationships the answer requires). Preserve the original entities, numbers, dates, and quoted phrases.
2. **Round 1 — three-channel recall.** For each information point, issue a retrieval call spanning all channels:

   ```
   mobile-retrieval_retrieve(query=<point + keywords>, scope="wiki", channels=["vector","keyword","graph"], top_k=5, include_content=true)
   ```

   For a point about provenance or original wording, add a `scope="source"` call (the graph channel is auto-disabled for source scope; and since the vector index covers wiki pages only, source recall is keyword-driven — use specific terms/names). For a point about relationships or neighbours, you may instead call `mobile-retrieval_graph_neighbors(seed=<page_id or title>, depth=1, top_k=10)`.
3. **Judge sufficiency.** Inspect every returned snippet, content block, ID, and relationship. Mark each information point as supported or unsupported.
4. **Round 2 — targeted supplement (optional).** For still-unsupported points only, make one more focused round with a sharper query or a complementary channel/scope. Do not exceed two rounds total, and do not re-issue a query that only cosmetically differs from one already run.

## Answer from evidence

- Answer the original question, weaving the per-point evidence into one coherent response.
- Treat every returned `path` as a citation identifier, never a workspace file — no `read`, grep, or shell on returned paths. Get prose from `include_content=true` payloads.
- Cite the relative `path` with `page_id`/`source_id`/`chunk_id` when present. Separate retrieved facts from inference and label inference explicitly. Never invent pages, IDs, relationships, or sources.
- If after two rounds some information points remain unsupported, answer what the evidence supports, state the specific gap, and recommend the High tier for open-ended follow-up. Do not keep looping.
- Mention `embedding.status` degradation only when it affects confidence.
