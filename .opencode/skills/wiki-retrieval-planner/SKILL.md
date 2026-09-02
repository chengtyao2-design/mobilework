---
name: wiki-retrieval-planner
description: Plan complex local-Wiki retrieval by preserving the user's constraints, decomposing evidence needs, rewriting queries, and routing each need to retrieve, graph_neighbors, or knowledge_tree. Load before the Medium or High retrieval tier; do not use for Naive or Low.
metadata:
  audience: wiki-users
  role: retrieval-planner
---

# Wiki retrieval planner

Create an internal retrieval plan for the user's original question. Do not call tools and do not answer the question.

## Preserve the request

Keep exact entities, acronyms, numbers, dates, quoted phrases, negations, comparison axes, and requested output constraints. A rewritten query may remove conversational filler but must not broaden or change the claim being investigated.

## Plan the evidence

1. Classify the information need as fact, explanation, comparison, relationship, provenance/original wording, knowledge-base structure, or multi-hop synthesis.
2. Split it into independently verifiable information points only when that improves recall. Avoid several cosmetic variants of the same query.
3. For each point, determine:
   - a concise query with discriminative Chinese or English keywords;
   - `retrieve`, `graph_neighbors`, or `knowledge_tree`;
   - `scope="wiki"`, `scope="source"`, or `scope="both"`;
   - the useful subset of `vector`, `keyword`, and `graph` channels;
   - whether snippets suffice or `include_content=true` is needed;
   - what evidence would make the point supported.
4. Define the remaining evidence gaps and a stop condition.

Use `knowledge_tree` for inventory or when a graph seed is unknown, `graph_neighbors` for explicit relationships from a known page, `scope="source"` for provenance or original wording, and `retrieve` for ordinary semantic or exact-term recall. The vector index covers Wiki pages, so source-only searches need specific keyword-bearing queries.

## Hard boundaries

- The selected tier is a fixed execution budget. Never change it or silently escalate it.
- Medium may execute at most two rounds; High may execute at most five rounds.
- Do not call a retrieval tool, generate the final answer, or present inference as evidence.
- If the chosen tier cannot close a gap, leave the gap explicit for the tier Skill to report.
