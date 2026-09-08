# LLM Wiki 检索 Baseline 对比

## 1. 对比范围

本报告的主要对比对象是：

1. [Astro-Han/karpathy-llm-wiki](https://github.com/Astro-Han/karpathy-llm-wiki)：Karpathy LLM Wiki 思路的可运行 Skill 实现。
2. [nashsu/llm_wiki](https://github.com/nashsu/llm_wiki)：完整桌面产品实现。
3. Mobilework：本项目的多知识库 Agent 检索实现。

三方主实验使用相同的 50 个 Compiled Wiki 页面、相同 Query 和 Top-5 口径。nashsu 与 Mobilework 使用同一 OpenRouter `qwen/qwen3-embedding-8b` 的 4096 维 Query 向量；Mobilework 使用 Wiki-only。主表采用 `network_excluded` 口径比较本地检索实现，另设 `end_to_end` 组记录真实远程 Embedding 成本。没有在该口径下运行的数据不进入主结果表；失败不以模拟数据代替。

## 2. 技术演进

### Karpathy 可运行版：最小范式

Karpathy 路线的核心结构是：

```text
Raw Sources
    ↓ LLM Compile
Compiled Wiki
    ↓ Index / Skill
Query and cited answer
```

Astro-Han 的仓库将这一思路做成可运行 Skill。其价值是结构透明、依赖较少、适合作为最小 Baseline。查询主要依赖 Wiki Index、全文搜索和候选页面读取；它没有专门面向多知识库的路由、联邦融合和统一查询预算。

### nashsu：桌面产品化

nashsu 将摄取、增量维护、任务恢复、图关系、社区发现和可选向量检索整合进桌面应用。它代表从可运行 Skill 到持续维护知识库产品的工程化路线。主实验通过其原生本地 Search / Chat 接口执行，不为追求统一而替换其检索算法。

### Mobilework：多库与 Agent 调度

Mobilework 保留 Raw Sources → Compiled Wiki 的编译式结构，同时为每个知识库维护独立资料、配置、索引和生命周期状态。检索后端提供 Vector、Keyword、Graph 三通道与 RRF 融合；联邦层负责知识库路由、并行查询、故障隔离和全局排序；统一 Skill 负责问题拆解、证据充分性、冲突解释与停止决策。

## 3. 能力对比

| 维度 | Karpathy 可运行版 | nashsu | Mobilework |
|---|---|---|---|
| 主要定位 | 最小可运行 Wiki Skill | 桌面知识库产品 | Agent 驱动的多知识库检索系统 |
| 查询入口 | Index、全文搜索、页面读取 | 原生 Search / Chat | MCP 检索接口 |
| 检索能力 | 关键词与页面读取 | 词法、图关系、可选向量 | Vector、Keyword、Graph 与 RRF |
| 多知识库 | 无专门路由 | 以应用项目为边界 | 独立 KB、目录路由与联邦检索 |
| 调度方式 | 单 Skill 流程 | 应用内逻辑 | 统一 Skill + Profile + 后端守卫 |
| 增量维护 | 依赖 Skill 与目录约定 | Watcher、持久队列、恢复 | Manifest、Staging、校验与 Journal |
| 故障降级 | 无统一通道状态 | 由应用实现处理 | 向量失败后保留 Keyword/Graph，并返回降级状态 |
| 查询预算 | 无统一硬预算 | 产品内部控制 | Profile 调度，代码执行时间与调用上限 |
| 同语料实测 | 检索 13/36、回答 6/12 成功 | 向量检索 36/36、回答 12/12 成功 | 向量检索 36/36、回答 7/12 成功；Fast Skill 回归通过 |

能力表描述可验证的实现差异，不是准确率评分。图谱成熟度、部署便利性或功能数量不能替代同语料检索结果。

## 4. 三方实验协议

### 检索实验

使用 Q01、Q03、Q04、Q06、Q07、Q08、Q10、Q12、Q15、Q18、Q19、Q20。每个系统每题重复 3 次，比较 Hit@5、MRR、无关证据率和可比本地检索 p95。nashsu 与 Mobilework 在计时前共享完全相同的 Query 向量；计时内分别执行各自原生索引、图/关键词融合与排序。两者保留原生切块和融合算法，因此结论限定为“同模型、同语料、不同检索实现”。

### 端到端回答

使用 Q01、Q08、Q15、Q18、Q19、Q20。每个系统每题重复 2 次，统一回答模型、中文 Prompt、Top-5 证据和证据长度。评分事实覆盖、Citation Recall、Groundedness、冲突处理、拒答和陈旧事实错误。

### 复现记录

每条结果必须包含系统 ID、仓库 Commit、运行时间、Query、条件、重复序号、状态和原始候选。只记录真实结果；失败和不可用状态单独进入 Failures。

<!-- AUTO:BASELINE_RESULTS_START -->

| 系统 | Commit | 成功检索数 | Hit@5 | MRR | 检索 p95 | 回答质量 |
|---|---|---:|---:|---:|---:|---:|
| Astro-Han/karpathy-llm-wiki | `eafcc77001e496cc43499e4923b663aec722c813` | 13/36 | 76.9%* | 71.2%* | 63,331.7 ms* | 39.2%*（6/12 成功子集） |
| nashsu/llm_wiki | `e8082119649e6a8e1cf85eaf289adcabfdf39d4e` | 36/36 | 75.0% | 49.3% | 71.5 ms | 35.5%（12/12） |
| Mobilework | `e7ead96` | 36/36 | 75.0% | 59.7% | 44.9 ms | 28.5%*（7/12 成功子集） |

带星号的指标只基于成功子集，必须与成功覆盖率一起阅读。nashsu 与 Mobilework 的 Hit@5 均为 75.0%；Mobilework MRR 高 10.4 个百分点，本地 p95 低 37.2%。Karpathy 的成功子集质量较高，但 23/36 条检索仍失败，因此不能用 13 条成功样本直接宣称优于完整样本系统。

端到端组的检索 p95 为 Mobilework 20,161.5 ms、nashsu 13,664.7 ms；Embedding API p50/p95 分别约为 4,829.7/20,116.6 ms 与 2,199.8/13,599.1 ms。相较于 44.9/71.5 ms 的本地检索 p95，远程 Embedding 是当前部署时延的主要组成，但主表没有采用事后相减的估算值。

<!-- AUTO:BASELINE_RESULTS_END -->

## 5. 当前可复用的 Mobilework 结果

`multikb-vector-20260904` 共完成 364 次真实语料检索，其中 4 次发生通道降级。小样本结果如下：

| 配置 | 样本数 | Hit@5 | p95 延迟 |
|---|---:|---:|---:|
| Keyword | 20 | 80% | 0.032 秒 |
| Vector | 20 | 95% | 7.17 秒 |
| Vector + Keyword | 20 | 90% | 5.20 秒 |
| Vector + Keyword + Graph | 60 | 90% | 15.72 秒 |

这些数据用于 Mobilework 内部通道消融，不代表三方胜负。回答由确定性摘句产生，不能视为完整 Agent 回答质量。三通道样本数更多，网络状态随运行时间变化，第一条 Vector 请求还存在异常长等待。因此只能报告本批观测到的召回与延迟权衡。

`multikb-local-20260904` 仅用于无向量降级和本地延迟参考。旧 terminal profile 仅作为统一 Skill 改造前快照。历史结果与当前分支结果通过 provenance 标签分开。

## 6. Mobilework 的实验结论

本分支对这些主张的验证结果如下：

- 模糊 Query：Vector 在标准和口语问法上都达到 100% Hit@5，但 MRR 从 100% 降至 78.3%；只能说明召回稳定，不能说排序不受影响。
- 多库路由：自动、全库和 Gold KB 的 Hit@5 均为 87.5%；自动路由 p95 为 18.72 秒，较全库的 28.27 秒低 33.8%。无关证据率均为 0，因此只验证了延迟收益，没有验证污染率下降。
- 故障隔离：关闭向量和注入 Embedding 错误时，六道题均继续返回结果且 Hit@5 为 100%；MRR 从正常向量的 58.3% 降至 47.8%，验证了“可降级”，没有验证“质量不变”。
- 时间证据：F2/F3 的 Hit@5 从 87.5% 提至 100%，但人工陈旧事实错误率仍为 62.5%，该主张尚未通过。
- Agent 调度：Profile Reference 注入、旧配置兼容、预算守卫和工作簿公式共 13 个 Node 测试通过；真实 Agent 回归中 Fast-Q01 成功，其余 4 项仍失败。
- 知识缺口：Research-Q20 两次尝试后仍失败，暂不能声称拒答已被端到端验证。

OpenRouter Embedding 已在 Mobilework 检索重试中真实跑通。端到端 Qwen/OpenCode 超时是另一条链路的问题，不能把向量成功等同于 Agent 回归成功。

## 7. 相关工作补充：green-dalii

[green-dalii/obsidian-llm-wiki](https://github.com/green-dalii/obsidian-llm-wiki) 展示了 Obsidian 内的多阶段级联检索与 Personalized PageRank 路线，适合说明无 Embedding 的图感知检索。其公开成绩来自作者自有语料，语料、问题和运行环境与本次实验不同，因此放在相关工作部分，不进入 Karpathy、nashsu、Mobilework 的主对比表。

后续若把它加入统一实验，需要为 Obsidian 原生接口建立独立适配器，并保持同一 Wiki、Query 和 Top-5 口径。此前不报告跨语料排名。

## 8. 证据边界

- 三方结果只有在真实仓库成功运行后才写入；失败原因与成功次数同时展示。
- nashsu 与 Karpathy 的原生返回若不包含某项指标，该项显示 `n.a.`，不由其他指标推断。
- 远程模型、Embedding、冷/热缓存和网络波动会影响延迟，实验保留单次记录与 p95。
- 20 题题库是项目级小样本。重复运行衡量稳定性，不增加独立题目数。
- Hit@5 和 MRR 衡量检索，不等同于最终回答正确率。
- 新鲜度来源召回提升不等同于陈旧事实错误下降，后者必须依赖人工标签。
- nashsu 主实验已配置与 Mobilework 相同的 OpenRouter 向量模型，并要求每条检索 `vectorHits > 0`；旧 token+graph 的 36 条检索仅作为消融组保留（Hit@5 50.0%、MRR 37.5%、p95 57.9 ms），不参与主表胜负。
- Karpathy 与 Mobilework 的失败项按 180 秒上限独立重试；仍失败的记录保留为失败或不可用，没有补值。
