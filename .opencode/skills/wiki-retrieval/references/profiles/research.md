# Research profile

The injected configuration is authoritative. The canonical Research defaults are:

- channels: ["vector", "keyword", "graph"]
- decompose: true
- raw_evidence_fallback: true
- auto_supplement: true
- sufficiency_check: true
- max_tool_calls: 8
- max_subqueries: 3
- max_graph_depth: 2
- max_evidence_chars: 24000
- deadline_ms: 90000

Use iterative evidence gathering for difficult comparisons, provenance checks, temporal conflicts, and multi-hop questions. After each call, perform an explicit internal sufficiency check against every information need. Verify important wording against raw evidence when enabled, traverse at most two graph hops, and use the remaining budget only to close identified gaps. Refuse unsupported precision rather than inventing an answer.
