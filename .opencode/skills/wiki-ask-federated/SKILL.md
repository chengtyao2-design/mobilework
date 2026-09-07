---
name: wiki-ask-federated
description: Answer from selected local knowledge bases using catalog routing and unified retrieval, bounded by the injected profile and independent switches.
---

# Bounded federated retrieval

For a factual lookup (such as an acronym and its three categories), one successful retrieval containing the requested facts is sufficient in ALL four profiles. Immediately give the final answer; higher profiles allow more work but never require extra calls. A budget limits retrieval, not the right to answer. Always use the current turn's profile and enabled channels, not values remembered from an earlier turn. Request top_k=5 for simple factual questions and include_content=true.

For local evidence call `route_knowledge_bases` with the original query and user-selected `kb_ids` if any. Catalog routing does not consume a retrieval round. Then call unified `retrieve`, preserving the original selected KB boundary, with `profile` and nested `overrides.retrieval` / `overrides.budget` from the injected configuration. Backend routing handles parallel search and low-confidence/no-result fallback. Never expand outside explicitly selected KBs. One skill handles every KB.

Only use enabled channels. Source verification requires `raw_evidence_fallback`; graph traversal requires `graph`, cross-KB expansion additionally requires `cross_kb_entity_expand`. Decomposition, sufficiency checks and supplementary searches each require their own switch. Delegate rerank and freshness to backend. Writeback needs both its switch and explicit user authorization; if no authorized tool exists, offer a draft.

Track retrieval start time, calls, normalized queries, evidence IDs and remaining needs. Stop on evidence sufficient, deadline_ms, max_tool_calls, one round without new evidence, duplicate:true, or a semantically equivalent query already tried. Plugin normalization is an additional guard, not a semantic duplicate detector. Never evade a stop through rephrasing. Parallel subqueries count individually toward the call budget. Respect max_subqueries, max_graph_depth and max_evidence_chars.

Claim/chunk mapping is many-to-many. Use source dates, effective intervals, verification status and supersedes/contradicts links. Missing dates are unknown. For current questions prefer applicable verified evidence; historical questions retain the requested period. Newer publication alone does not invalidate earlier evidence. Pages citing one source are not independent corroboration.

Answer using human-readable document titles and KB provenance. Explain conflicts with their scopes, dates and sources. Separate unsupported inference from facts. If evidence is absent, identify the gap; never invent numerical effects. Disclose partial backend failures when they limit the answer. Do not mention the tier, tools, internal plans or IDs unless asked to debug.
