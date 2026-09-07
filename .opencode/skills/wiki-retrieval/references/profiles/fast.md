# Fast profile

The injected configuration is authoritative. The canonical Fast defaults are:

- channels: ["vector"]
- scope: `wiki`
- decompose: false
- auto_supplement: false
- max_tool_calls: 1
- max_subqueries: 1
- max_graph_depth: 0
- max_evidence_chars: 6000
- deadline_ms: 5000

Make one semantic retrieval call and then answer. Do not decompose, traverse the graph, search raw sources, or retry. If embeddings are degraded or disabled, state that the available evidence cannot support an answer; do not silently add another channel.
