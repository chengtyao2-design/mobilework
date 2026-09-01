---
name: wiki-ask-low
description: Answer a question from this local knowledge base at the Low retrieval tier — a single hybrid recall fusing the vector and keyword channels through RRF, with no query decomposition and no follow-up calls. Use when one fused pass over semantics plus exact terms should answer the question, or when a benchmark explicitly selects the Low tier.
metadata:
  audience: wiki-users
  retrieval-tier: low
---

# Wiki Ask — Low tier

Answer using only the `mobile-retrieval` MCP server. One hybrid recall (vector + keyword, fused by RRF); no decomposition, no fallback loop.

## Procedure

1. Preserve the original question. Rewrite it into one concise retrieval query and extract 3–8 discriminative keywords (separate Chinese keywords with spaces; keep exact technical names such as RRF, LanceDB, Qwen3). Fold the keywords into the query string.
2. Call the retrieval tool exactly once with both channels:

   ```
   mobile-retrieval_retrieve(query=<rewritten query + keywords>, scope="wiki", channels=["vector","keyword"], top_k=5, include_content=true)
   ```

   Keep `rrf_k=60` unless the user explicitly requests a fusion experiment. Set `verbose=true` only when the user asks for per-channel ranks or latency.
3. Do not decompose the question, do not call a second tool, and do not repeat the call with cosmetic variations.

## Answer from evidence

- Answer the original question, not the rewritten query.
- Treat every returned `path` as a citation identifier, never a workspace file — no `read`, grep, or shell on returned paths. Get prose from the `include_content=true` payload.
- Cite the relative `path` with `page_id` when present. The fused ranking already blends semantic and exact-term evidence; prefer top-ranked results but read their snippets before asserting.
- Separate retrieved facts from inference and label inference. Never invent pages, IDs, or relationships.
- If results are empty or off-target after inspecting all of them, say so and recommend the Medium tier (which decomposes the question and adds the graph channel). Do not open a retrieval loop here.
- If `embedding.status` is `degraded`/`disabled`, the keyword channel still contributes; note the reduced confidence rather than failing.
