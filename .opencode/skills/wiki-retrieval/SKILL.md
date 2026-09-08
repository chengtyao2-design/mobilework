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

Call `route_knowledge_bases` with the original query and user-selected `kb_ids`, if any. Catalog routing does not consume a retrieval round. Then call unified `retrieve`, preserving the selected KB boundary, passing `retrieval_profile` as `profile` and the injected retrieval/budget values as overrides. Backend routing owns parallel KB search, fusion, and low-confidence or no-result fallback. Never expand beyond explicitly selected knowledge bases.

Use only enabled channels. Request `top_k=5` and `include_content=true` for ordinary factual questions. Source verification requires `raw_evidence_fallback`; graph traversal requires `graph`; decomposition, sufficiency checks, and supplementary searches require their corresponding switches. Delegate rank fusion and freshness handling to the backend.

After every call, inspect returned evidence text, identifiers, relationships, dates, and failure metadata. Mark each information need supported or unsupported. When supplementary retrieval is allowed, target only a remaining gap with a substantively different query, scope, or route.

## Budgets and stopping

Treat `deadline_ms`, `max_tool_calls`, `max_subqueries`, `max_graph_depth`, and `max_evidence_chars` as hard limits. Track elapsed time, calls, normalized/semantic queries, evidence IDs, and remaining gaps. Parallel subqueries count individually toward the call budget.

Stop immediately when evidence is sufficient, a hard limit is reached, a round yields no new evidence, the backend returns `duplicate:true`, or a semantically equivalent query was already tried. Never evade a stop by cosmetic rephrasing. A higher budget permits more work but does not require it; one successful retrieval is enough for a simple fact in every profile.

## Answer from evidence

Answer the original question using human-readable document titles and KB provenance. Treat returned paths as citation identifiers, not workspace files; obtain prose only from returned evidence content. Do not invent pages, sources, relationships, or numerical effects.

Claim-to-chunk mapping is many-to-many. Use source dates, effective intervals, verification status, and supersedes/contradicts links. Missing dates are unknown; a newer publication alone does not invalidate earlier evidence. Pages derived from the same original document are not independent corroboration.

Explain conflicts through scope, date, and source. Separate inference from retrieved facts. If evidence is absent or incomplete, answer only the supported portion and state the specific gap. Disclose partial backend failures only when they materially limit the answer.

Keep the plan and evidence bookkeeping private. Do not mention the profile, Skill, tools, channels, scopes, parameters, call counts, or internal IDs unless the user explicitly asks to debug retrieval.
