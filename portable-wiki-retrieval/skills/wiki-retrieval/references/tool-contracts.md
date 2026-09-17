# 工具合同

## 常规检索

`retrieve` 是普通问答的首选入口。传入原始 `query`、档位、可选 `wiki_ids`、最小 `context`、高层 `retrieval_hints` 和可选 `information_needs`。不要预先改写 Query。

普通事实问答应直接调用一次 `retrieve`，不先调用 `prepare_query` 或 `get_settings`。`retrieval_hints` 可以省略；若传 `intent`，仅允许：`exact`、`semantic`、`entity`、`relationship`、`temporal`、`comparison`、`provenance`、`inventory`。没有把握时省略，不要猜测新值。`information_needs` 仅用于多个独立信息需求。

返回中的 `query_plan` 是实际执行计划；`results`/`evidence` 是唯一可用于回答的事实来源。引用时至少保留 `wiki_id`、`relative_path`、`title` 和 `heading`。同时检查 `degradations`、`partial_failure`、`stop_reason`、索引和模型状态。

## 管理工具

- `list_wikis`：查看已注册 Wiki 和索引状态；它不是本地文件系统发现工具。
- `inspect_wiki`：只读预检用户给出的确切目录，不负责搜索或猜测目录。
- `register_wiki`：用户明确要求后注册并创建索引。
- `update_wiki`：更新注册信息；返回需要重建时再调用相应维护操作。
- `unregister_wiki`：删除注册记录和派生索引，不删除 Wiki 文件。
- `knowledge_tree`：浏览已索引文档和稳定 ID。
- `graph_neighbors`：围绕稳定文档 seed 做有界扩展，不能把自然语言 Query 当 seed。

模糊的“另一个资料目录”不能映射成 Wiki。若 `list_wikis` 中没有唯一匹配项，应请求用户提供确切根目录；禁止借助客户端的 Read、Glob、Grep、文件搜索或 shell 枚举本机路径。确切路径只授权 `inspect_wiki`；注册仍需用户明确表达注册意图。注册后的内容检索不得绕过 MCP 直接读文件。

## 配置工具

`get_settings`、`update_settings` 和 `reset_settings` 读写全局、档位或 Wiki 覆盖。字段名保持英文；密钥不经过这些工具传递，只通过 `OPENROUTER_API_KEY` 环境变量或本地 `.env` 提供。

工具失败时读取稳定错误码。常见码包括 `INVALID_ARGUMENT`、`WIKI_NOT_FOUND`、`INDEX_STALE`、`VECTOR_UNAVAILABLE`、`RERANK_UNAVAILABLE`、`ENTITY_EXPANSION_UNAVAILABLE`、`BUDGET_EXHAUSTED` 和 `DUPLICATE_QUERY`。不要将失败推断为“没有相关事实”。
