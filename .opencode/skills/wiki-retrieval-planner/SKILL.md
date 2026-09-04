---
name: wiki-retrieval-planner
description: Plan local multi-knowledge-base retrieval within the injected profile and independent switches before unified execution.
metadata:
  audience: wiki-users
  role: retrieval-planner
---

# Wiki retrieval planner

Create a brief internal retrieval plan for the user's original question. Planning is only the first stage of this same turn: immediately continue with the injected wiki-ask-federated execution instructions, call the required tools, and answer the user. For a single-fact question, proceed directly to catalog routing and retrieval without extended planning.

The plan is private execution state. Never print, summarize, announce, or otherwise expose it to the user. Never call the `skill` tool; Mobilework has already injected the correct ordered Skill set for this turn.

## Preserve the request

Keep exact entities, acronyms, numbers, dates, quoted phrases, negations, comparison axes, and requested output constraints. A rewritten query may remove conversational filler but must not broaden or change the claim being investigated.

## Plan the evidence

1. Classify the information need as fact, explanation, comparison, relationship, provenance/original wording, knowledge-base structure, or multi-hop synthesis.
2. Split into independently verifiable points only if decompose is enabled, within max_subqueries. Avoid cosmetic variants of one query.
3. For each point, determine:
   - a concise query with discriminative Chinese or English keywords;
   - `retrieve`, `graph_neighbors`, or `knowledge_tree`;
   - `scope="wiki"`, `scope="source"`, or `scope="both"`;
   - the useful subset of `vector`, `keyword`, and `graph` channels;
   - whether snippets suffice or `include_content=true` is needed;
   - what evidence would make the point supported.
4. Define the remaining evidence gaps and a stop condition.

Use knowledge_tree for inventory, graph_neighbors only when graph is enabled, source scope for provenance, and retrieve for semantic or exact-term recall. Wiki and source chunks can both be indexed. Preserve the user's kb_ids boundary. The execution skill calls route_knowledge_bases first; backend owns parallel KB search and fusion.

## Hard boundaries

- The selected tier is a fixed execution budget. Never change it or silently escalate it.
- Use the injected deadline_ms, max_tool_calls, max_subqueries, max_graph_depth and max_evidence_chars; legacy tier rounds do not override these budgets.
- During planning only, do not present inference as evidence. After planning, execute retrieval and generate the final answer in this same turn.
- If the chosen tier cannot close a gap, leave the gap explicit for the tier Skill to report.
