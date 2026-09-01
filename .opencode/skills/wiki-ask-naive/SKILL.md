---
name: wiki-ask-naive
description: Answer a question from this local knowledge base at the Naive retrieval tier — a single pure-vector recall with no query decomposition and no follow-up calls. Use for fast, fact-style lookups where one semantic pass is expected to surface the answer, or when a benchmark explicitly selects the Naive tier.
metadata:
  audience: wiki-users
  retrieval-tier: naive
---

# Wiki Ask — Naive tier

Answer using only the `mobile-retrieval` MCP server. This is the shallowest tier: exactly one vector recall, no decomposition, no fallback.

## Procedure

1. Keep the user's original question intact — its entities, acronyms, numbers, dates, and quoted phrases. Rewrite it into one concise retrieval query, removing only conversational filler.
2. Call the retrieval tool exactly once:

   ```
   mobile-retrieval_retrieve(query=<rewritten query>, scope="wiki", channels=["vector"], top_k=3, include_content=true)
   ```

   Keep `rrf_k=60`. Do not set `verbose` unless the user asks for latency or channel diagnostics.
3. Do not call any other tool, do not retry with reworded queries, and do not open a retrieval loop. One call only.

## Answer from evidence

- Answer the original question, not the rewritten query.
- Treat every returned `path` as a knowledge-base citation identifier, never a file in the current workspace. Do not `read`, grep, or shell out to a returned path; get prose from the `include_content=true` payload.
- Cite the returned relative `path` and include `page_id` when present. Separate retrieved facts from inference and label inference explicitly. Never invent pages, IDs, or relationships the tool did not return.
- If the single result is empty or off-target, say so plainly and suggest a narrower question or a higher retrieval tier — do not silently make another call.
- If `embedding.status` is `degraded` or `disabled`, the vector channel returned nothing useful; state that the Naive tier cannot answer without embeddings and recommend the Low tier (which adds the keyword channel).
