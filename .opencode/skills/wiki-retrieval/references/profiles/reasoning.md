# Reasoning profile

The injected configuration is authoritative. The canonical Reasoning defaults are:

- channels: ["vector", "keyword", "graph"]
- decompose: true
- raw_evidence_fallback: true
- auto_supplement: true
- sufficiency_check: false
- max_tool_calls: 4
- max_subqueries: 3
- max_graph_depth: 1
- max_evidence_chars: 18000
- deadline_ms: 30000

Decompose only genuinely multi-part questions. Execute the highest-value subqueries first, then make targeted supplementary calls only for unsupported points. Graph traversal is limited to one hop. Stop as soon as the original question is supported; the four-call budget is a ceiling, not a target.
