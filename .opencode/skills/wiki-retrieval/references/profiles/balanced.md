# Balanced profile

The injected configuration is authoritative. The canonical Balanced defaults are:

- channels: ["vector", "keyword"]
- scope: `wiki`, or `both` in the same call when raw evidence is required
- decompose: false
- raw_evidence_fallback: true
- auto_supplement: false
- max_tool_calls: 1
- max_subqueries: 1
- max_graph_depth: 0
- max_evidence_chars: 12000
- deadline_ms: 12000

Make one hybrid retrieval call and then answer. Fold 3–8 discriminative keywords into the preserved query. Do not decompose or open a follow-up loop. If embeddings degrade, keyword evidence may still support the answer; disclose reduced confidence only when material.
