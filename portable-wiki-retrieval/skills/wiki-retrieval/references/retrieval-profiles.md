# 检索档位

所有具体预算、允许通道和模型状态都应通过 `retrieval_capabilities` 或 `get_settings` 读取，不要在提示中硬编码。

- `fast`：低延迟事实检索。通常只有一个主分支；关键词与向量是同一分支内的并行通道。向量不可用时允许降级为关键词。
- `balanced`：默认档位。适合大多数单轮 Wiki 问答，由 MCP 按意图决定是否使用有限辅助能力。
- `reasoning`：适合关系、多跳或多个独立信息需求。仅证据确有缺口时允许携带同一 `run_id` 补检。
- `research`：适合更宽的证据收集和跨 Wiki 场景。语义实体扩展仍受能力与评测闸门约束。

时间分支不会每次触发。只有 Query 同时明确表达当前与历史，或明确要求历史比较时，MCP 才生成对应分支。普通“现在如何”仍保持一条主分支。

`rerank_mode=auto` 失败时继续使用融合结果；`required` 失败时返回错误。`entity_expand_mode=semantic` 闸门未通过时降级到 `conservative`；`semantic_required` 不允许降级。
