---
name: wiki-retrieval
description: 当用户要求从已经注册的本地 Markdown Wiki、知识库、项目文档或制度库中查找事实、历史版本、页面关系或文件依据时，调用检索 MCP 并只依据证据回答。不用于普通常识问答；未经用户明确要求不注册 Wiki。
---

# Wiki 检索

## 工作原则

先判断问题是否需要用户已注册 Wiki 中的事实。普通知识问题不要触发本 Skill。注册、更新或注销 Wiki 必须来自用户明确意图；检索问题不得自行注册目录，也不得直接读取路径绕过 MCP。

用户不必显式点名本 Skill。问题明显要求查询已注册 Wiki 或知识库时直接按本流程工作。

配置和具体数值以 MCP 的有效设置为唯一真源，不在 Skill 中复制模型名、阈值、权重或预算。

## 检索步骤

1. 用户没有指定档位时选择 `balanced`；尊重用户明确指定的 `fast`、`balanced`、`reasoning` 或 `research`。
2. 用户指定 Wiki 时，将其 ID 原样放入 `wiki_ids`，形成硬边界。没有指定时，只能在 `list_wikis` 返回的已注册 Wiki 中让 MCP 路由。
3. 从当前对话只提取完成问题所需的最小上下文：已明确的实体、时间焦点和少量前序主题。歧义实体只作为待确认线索，不描述成已确认实体。
4. 普通问答直接调用一次 `retrieve`。不要先调用 `prepare_query`、`get_settings`，也不要为了查参数临时读取 references。
5. `retrieval_hints` 是可选项。只有意图非常明确时才传 `intent`，且值只能是 `exact`、`semantic`、`entity`、`relationship`、`temporal`、`comparison`、`provenance`、`inventory`；拿不准时完全省略，由 MCP 判断。不得创造 `fact_lookup`、`lookup` 等值。
6. 只有问题确实包含多个彼此独立的信息需求时才传 `information_needs`；普通事实问答省略。不要自行编写完整 query plan，也不要生成改写 Query。
7. 同一次 `retrieve` 由 MCP 决定是否增加有限辅助分支，以及在允许通道中执行关键词、向量、实体或图召回。若可选参数导致 `INVALID_ARGUMENT`，移除该可选参数后最多重试一次，不要转而探测配置。
8. 只有 `reasoning` 或 `research` 且首轮 Evidence 存在明确缺口时才补检。补检必须携带首轮返回的同一个 `run_id`，并避免等价 Query。
9. 只依据返回的 `evidence` 回答；每项关键结论引用返回的 Wiki ID、路径和 heading。若证据不足、降级或部分失败，应明确说明。

## 工具边界

- 查询规划由 MCP 唯一生成。只有用户明确要求预览或解释计划时才调用 `prepare_query`；它不是普通检索的前置调用。
- `graph_neighbors` 仅接受已有结果提供的稳定 seed；seed 至少含 `wiki_id`，并能解析到 `document_id`。
- `list_wikis` 只用于查看已注册 Wiki，不能用来发现电脑中的目录。用户所说的 Wiki 名称无法与已注册项唯一对应时，停止并请用户选择 `wiki_id`。
- 用户只说“电脑里还有一个目录”“另一个项目资料”等模糊位置时，最多调用一次 `list_wikis`。若无法匹配，立即请用户提供确切根目录；不得使用 Read、Glob、Grep、文件搜索、shell 或 PowerShell 枚举桌面、文档、磁盘或父目录来猜测位置，也不得读取候选目录内容。
- 用户提供确切目录并要求检查时，可以调用 `inspect_wiki`，但这不等于授权注册。只有用户明确表达“注册”“添加为 Wiki/知识库”等意图后才调用 `register_wiki`。
- 已注册 Wiki 的检索内容只能经检索 MCP 取得，不得直接读取其文件路径绕过 MCP。
- 不硬编码任何客户端特有的工具前缀；按当前环境暴露的工具名调用。
- 不在输出、日志或 Skill 中写入密钥、用户绝对路径或模型参数。

档位和降级语义见 [检索档位](references/retrieval-profiles.md)。输入输出与稳定错误见 [工具合同](references/tool-contracts.md)。
