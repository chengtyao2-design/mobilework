# 多库检索开关

`/retrieve` 选择 Fast（5 秒/1 次）、Balanced（12 秒/1 次）、Reasoning（30 秒/4 次）、Research（90 秒/8 次）。切换档位会清空高级覆盖。`/retrieve-options` 逐项切换能力，包括 Claim 新鲜度 off/context/rerank/both。

应用配置保存于 `.mobilework-state/preferences.json`。它包含 `retrieval_profile`、`retrieval` 和 `budget`；预算字段为 deadline_ms、max_tool_calls、max_subqueries、max_graph_depth、max_evidence_chars。优先级为单次请求 > 保存覆盖 > 档位默认。客户端单次请求可附加 `<mobilework-retrieval>{"retrieval":{"graph":false}}</mobilework-retrieval>`。

Balanced 默认 vector、keyword、raw_evidence_fallback 开启，其他布尔能力关闭，新鲜度 off。开启 source fallback 不增加预算：仍需在本轮剩余预算内执行。高级能力是否有效由真实消融实验判断，配置开启不构成效果承诺。answer_writeback 仍要求用户明确授权，不自动写库。

后端负责目录路由、并行检索和加权 RRF；MCP 暴露统一接口；Skill 负责选择信息需要、解释证据与冲突。每个 KB 维护自己的 wiki/index.md，不复制 Skill。显式 kb_ids 不可越界，目录无命中不代表知识缺失，后端应回退候选库。

构建和预览从应用根启动：`python -m wiki_maintainer --root <应用根> --kb-root <MOBILEWORK_KB_ROOT> ...`。应用根负责加载凭据，KB 根只限定内容、状态与索引；不要把应用 `--root` 直接替换为 KB 根。

停止条件包括充分证据、时间、调用上限、无新增证据和实质重复。插件强制字符串归一化重复、调用上限及下一次调用前的时间检查；语义重复由 Skill/后端判定。运行中的远程请求取消依赖后端超时，不应把插件的前置检查误认为可抢占超时。

Claim 是语义断言，与 Chunk 多对多关联。历史问题按指定时间回答；新鲜度不等于正确性。缺失日期视为未知，同源转载不能当作独立支持；冲突需说明来源、适用范围与日期。

高级开关现在显示中文：向量检索、关键词检索、图关系扩展；问题分解、原始资料回查、证据充分性检查和自动补检标注“Agent 执行”。模型重排、跨库实体扩展、回答写回标注“暂未接入”，不可启用，旧配置中的 true 也不会生效。界面阻止关闭最后一种检索通道。断言新鲜度的四种模式使用中文名称。

无进展检测在 15 秒提示当前是否已取得资料，连续 45 秒无消息/工具进展后停止响应并提供重试。已有检索时可以复用证据；首次模型响应尚未返回时重新处理原问题。正常流式输出或工具进展会刷新计时，模型重试状态不会取消检测。修改后需退出并重新启动 Mobilework 以加载新版 TUI 插件。

测试：`node --experimental-strip-types --test tests/retrieval_config.test.mjs`（Node 22+），以及 `python -m pytest tests/test_mobilework_cli.py`。
