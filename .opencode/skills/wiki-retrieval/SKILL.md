---
name: wiki-retrieval
description: Plan and execute bounded retrieval across local knowledge bases using the injected profile reference and configuration.
metadata:
  audience: wiki-users
  role: retrieval
---

# Unified Wiki retrieval

Use this Skill for all four retrieval profiles. The injected configuration is the executable source of truth; the accompanying profile reference explains the intended behavior. Never change or silently escalate the profile, enabled switches, knowledge-base boundary, or budget. Never call the `skill` tool because Mobilework has already loaded this Skill and exactly one profile reference.

Use only the reference matching `retrieval_profile`: [Fast](references/profiles/fast.md), [Balanced](references/profiles/balanced.md), [Reasoning](references/profiles/reasoning.md), or [Research](references/profiles/research.md). Do not load or combine the other profiles.

If the message is a greeting, UI/help request, or does not ask about the local knowledge bases, answer normally without retrieval. Otherwise keep planning private and concise. Continue immediately from planning to retrieval and answer in the same turn.

## Plan the evidence

Preserve exact entities, acronyms, numbers, dates, quoted phrases, negations, comparison axes, and requested output constraints. Remove conversational filler only; do not broaden the claim.

Classify the need as fact, explanation, comparison, relationship, provenance/original wording, inventory, or multi-hop synthesis. Split it into independently verifiable subqueries only when `decompose` is enabled and never exceed `max_subqueries`. For each subquery select the useful scope (`wiki`, `source`, or `both`), enabled channels, evidence detail, and success condition. Use `knowledge_tree` for inventory or seed discovery, `graph_neighbors` only when graph is enabled, source scope for provenance, and `retrieve` for semantic or exact-term recall.

## Route and retrieve

For ordinary questions call unified `retrieve` directly. If the user selected knowledge bases, pass the complete `kb_ids` boundary; otherwise omit `kb_ids` and let `retrieve` perform catalog routing internally. Do not call `list_knowledge_bases` or `route_knowledge_bases` as a preflight step: use them only when the user explicitly asks to inspect available knowledge bases or debug routing. For one subquery, make exactly one `retrieve` call whose `kb_ids` array contains every selected knowledge base; never make one call per knowledge base because the backend already searches them in parallel and performs global fusion. The runtime injects `profile`, `channels`, and nested `overrides`; do not construct or pass those arguments yourself. Never expand beyond explicitly selected knowledge bases.

Use only enabled channels. Request `top_k=5` and `include_content=true` for ordinary factual questions. Source verification requires `raw_evidence_fallback`; graph traversal requires `graph`; decomposition, sufficiency checks, and supplementary searches require their corresponding switches. In a multi-KB project, every `graph_neighbors` call must pass the `kb_id` from its seed retrieval hit; never pass a path-like seed without its knowledge-base boundary. Delegate rank fusion and freshness handling to the backend.

After every call, inspect returned evidence text, identifiers, relationships, dates, and failure metadata. Mark each information need supported or unsupported. When supplementary retrieval is allowed, target only a remaining gap with a substantively different query, scope, or route.

## Budgets and stopping

Treat `deadline_ms`, `max_tool_calls`, `max_subqueries`, `max_graph_depth`, and `max_evidence_chars` as hard limits. Track elapsed time, calls, normalized/semantic queries, evidence IDs, and remaining gaps. Parallel subqueries count individually toward the call budget.

Stop immediately when evidence is sufficient, a hard limit is reached, a round yields no new evidence, the backend returns `duplicate:true`, or a semantically equivalent query was already tried. Never evade a stop by cosmetic rephrasing. A higher budget permits more work but does not require it; one successful retrieval is enough for a simple fact in every profile.

## Answer from evidence

Every factual answer based on retrieved evidence must be traceable. Add bracketed citations such as `[1]` immediately after the sentence or bullet they support, and finish with a `参考证据` section mapping each number to the returned citation metadata: knowledge-base name, human-readable document titles, evidence type (Wiki 摘要 or 原始资料), and any available publisher, publication/update/effective date, derived-from title, and public source URL. Cite separate knowledge bases separately for cross-KB claims. If retrieval returned usable evidence, never omit this section; if metadata is absent, omit that field rather than inventing it. Treat returned paths as citation identifiers, not workspace files; obtain prose only from returned evidence content. Do not invent pages, sources, relationships, or numerical effects.

Claim-to-chunk mapping is many-to-many. Use source dates, effective intervals, verification status, and supersedes/contradicts links. Missing dates are unknown; a newer publication alone does not invalidate earlier evidence. Pages derived from the same original document are not independent corroboration.

Explain conflicts through scope, date, and source. Separate inference from retrieved facts. If evidence is absent or incomplete, answer only the supported portion and state the specific gap. Disclose partial backend failures only when they materially limit the answer.

Keep the plan and evidence bookkeeping private. Do not mention the profile, Skill, tools, channels, scopes, parameters, call counts, or internal IDs unless the user explicitly asks to debug retrieval.
