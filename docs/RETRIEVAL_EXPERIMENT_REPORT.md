# 多知识库检索实验与汇报方案

> 状态：2026-09-08 实验已收口。本文与最终 Excel 基于 1,658 条真实尝试：1,244 条当前有效记录、414 条已被成功或更新尝试替代的审计记录；当前有效记录中含 46 条失败，其中 8 条为明确隔离的历史 Skill 快照。失败不补零，也不用模拟值替代。

## 1. 汇报结论

本项目沿着三步演进展开：Karpathy 的可运行 LLM Wiki 给出 Raw Sources → Compiled Wiki → Skill 的最小范式；nashsu 将摄取、增量维护、图关系和检索整合为桌面产品；Mobilework 在此基础上处理多知识库隔离、路由、并行检索、通道降级、时间证据和分级 Agent 调度。

本次实验不把“模块更多”直接解释为“效果更好”。所有结论分别回答三个问题：是否找到正确证据、是否减少无关或陈旧证据、为此付出了多少延迟和工具调用。简单事实、口语改写、跨库综合、时间冲突、向量故障和知识缺口分别设置对照条件。

<!-- AUTO:EXECUTIVE_SUMMARY_START -->

1. 在同一 OpenRouter `qwen/qwen3-embedding-8b`、同一 50 页 Wiki、同一 Query 和 Top-5 下，nashsu 与 Mobilework 的主检索均完成 36/36 次，Hit@5 均为 75.0%；Mobilework MRR 为 59.7%，比 nashsu 的 49.3% 高 10.4 个百分点，本地检索 p95 为 44.9 ms，比 nashsu 的 71.5 ms 低 37.2%。Karpathy 成功 13/36 次，成功子集 Hit@5 为 76.9%，但成功覆盖不足，不能直接据此排名。
2. 模糊问法下，Vector 的 Hit@5 保持 100%，MRR 从 100% 降至 78.3%；Keyword 的 Hit@5 从 66.7% 降至 33.3%。自动路由与全库检索均为 87.5% Hit@5，但自动路由 p95 为 18.72 秒，较全库 28.27 秒低 33.8%。
3. 关闭向量和注入 Embedding 错误均未令六道题整体失败，Hit@5 仍为 100%，但 MRR 从 58.3% 降至 47.8%。端到端检索 p95 为 Mobilework 20.16 秒、nashsu 13.66 秒，而排除网络后的本地 p95 都低于 72 ms，说明当前部署时延主要来自远程 Embedding 与网络波动；两种口径严格分栏，不再事后估算相减。

<!-- AUTO:EXECUTIVE_SUMMARY_END -->

## 2. 三个主要对比系统

| 系统 | 在汇报中的定位 | 原生检索入口 | 本次实验角色 |
|---|---|---|---|
| [Astro-Han/karpathy-llm-wiki](https://github.com/Astro-Han/karpathy-llm-wiki) | Karpathy LLM Wiki 思路的可运行 Skill 实现 | Index、全文搜索、候选页面读取 | 最小可运行 Baseline |
| [nashsu/llm_wiki](https://github.com/nashsu/llm_wiki) | 带增量维护、图关系和可选向量能力的桌面产品 | 原生本地 Search / Chat 接口 | 工程化产品 Baseline |
| Mobilework | 多知识库检索后端与 Agent/Skill 调度 | MCP `retrieve` 等接口 | 本项目 |

green-dalii/obsidian-llm-wiki 保留在相关工作中，用于说明无 Embedding 的级联检索与 Personalized PageRank 路线。它不进入三方主结果表，也不把其自有语料公开数字与本次同语料实验混合比较。

## 3. 本分支改造：统一 Skill 与分级 Profile

检索行为收敛到一个 `wiki-retrieval` Skill。主 Skill 保存查询规划、知识库边界、证据判断、冲突处理、停止条件和回答约束；Fast、Balanced、Reasoning、Research 的差异放入 Profile Reference。

| Profile | 默认通道与行为 | 最大工具调用 | 主要使用场景 |
|---|---|---:|---|
| Fast | 单次向量检索 | 1 | 简单事实与低等待场景 |
| Balanced | 向量 + 关键词，单轮取证 | 1 | 普通问答 |
| Reasoning | 允许图扩展、问题分解和定向补检 | 4 | 流程、关系和组合问题 |
| Research | 增加充分性检查和原文核验 | 8 | 时间冲突、跨库研究和知识缺口 |

Reference 只指导 Agent 决策。Profile、通道、时间、调用次数、证据字符数、重复 Query 和无新增证据停止条件仍由配置、调用守卫和检索后端执行。这样可以统一规则来源，同时避免把预算约束只交给提示词。

## 4. 实验口径

### 4.1 语料与公平性

- 三方使用同一批 50 个 Compiled Wiki 页面，Top-K 固定为 5。Mobilework 主对比明确使用 `scope=wiki`，不再把 Raw Sources 混入同语料指标。
- 主检索实验不重新构建 Wiki，避免把摄取和编译质量混入检索比较。
- 端到端回答统一使用 `openrouter/qwen/qwen3.8-flash`、中文问题和相同证据上限。
- 每条记录保存系统、仓库 Commit、Query、条件、重复序号、状态、候选、指标和延迟。nashsu 与 Mobilework 主检索均使用同一 OpenRouter 客户端预计算的 4096 维 Query 向量；nashsu 通过 `queryEmbedding`，Mobilework 通过检索后端注入同一向量。计时区间只包含各自本地检索、融合和排序。另设 `end_to_end` 组，让两套系统分别在线请求同一模型并把网络耗时计入总时延。
- Baseline 原生接口不返回某项指标时记为 `n.a.`。失败记入 Failures，不记为 0。
- 历史结果带 `historical_result` 标签，与 `current_run` 分开汇总。

### 4.2 指标

| 指标 | 含义 |
|---|---|
| Hit@5 | 前五条证据是否包含题库指定来源 |
| MRR | 第一个正确来源的倒数排名 |
| Citation Recall | 回答引用覆盖预期来源的比例 |
| Irrelevant Evidence Rate | Top-5 中不属于目标证据范围的比例 |
| Answer Quality | 对事实、引用、证据支撑、冲突处理和拒答的加权分数 |
| Stale Fact Error | 回答是否采用已失效或错误时间范围的事实 |
| p95 Latency | 同一实验条件下的第 95 百分位耗时 |

Answer Quality 权重为：事实正确 35%、引用召回 25%、Groundedness 20%、冲突处理 10%、拒答 10%。不适用或未评分项从分母中移除，不视为零分。

## 5. 实验设计

### 5.1 三方同 Wiki 检索

使用 Q01、Q03、Q04、Q06、Q07、Q08、Q10、Q12、Q15、Q18、Q19、Q20。每个系统每题运行 3 次，计划 108 次。比较 Hit@5、MRR、Citation Recall、无关证据率及可比本地检索 p95。远程 Embedding 端到端延迟单独保留为部署环境指标，不参与检索引擎 Baseline 排名。

<!-- AUTO:SYSTEM_COMPARISON_START -->

| 系统 | 成功次数 | Hit@5 | MRR | p95 延迟 | 备注 |
|---|---:|---:|---:|---:|---|
| Karpathy 可运行版 | 13/36 | 76.9% | 71.2% | 63,331.7 ms | 指标只计算成功子集；23 条在两次独立尝试后仍失败，成功覆盖不足，不能与完整样本直接排名 |
| nashsu | 36/36 | 75.0% | 49.3% | 71.5 ms | 相同 OpenRouter Query 向量；`vectorHits > 0` 为成功必要条件 |
| Mobilework | 36/36 | 75.0% | 59.7% | 44.9 ms | Wiki-only；相同 Query 向量；真实执行 LanceDB、关键词、图、路由与融合 |

`network_excluded` 是主检索表口径，避免把同一个远程服务的网络波动误算成检索算法差异。独立 `end_to_end` 组中，Mobilework p95 为 20,161.5 ms，nashsu 为 13,664.7 ms；对应 Embedding API p50/p95 分别约为 4,829.7/20,116.6 ms 与 2,199.8/13,599.1 ms。本批观测表明远程 Embedding 占大部分部署时延，但不能把两组独立请求逐条事后相减作为“真实本地时延”。

<!-- AUTO:SYSTEM_COMPARISON_END -->

### 5.2 三方端到端回答

使用 Q01、Q08、Q15、Q18、Q19、Q20。每个系统每题运行 2 次，计划 36 次。除检索指标外，评分 Required/Forbidden Facts、引用正确性、冲突解释、时间边界和知识缺口拒答。

真实执行结果中，nashsu 12/12 次成功，Karpathy 6/12、Mobilework 7/12。只对成功运行计算的 Answer Quality 分别为 35.5%、39.2% 和 28.5%，Citation Recall 分别为 45.8%、66.7% 和 42.9%。Karpathy 与 Mobilework 存在大量失败，且 nashsu `/chat` 是原生接口输出，因此这组数值必须和成功覆盖率一起展示，不能单凭成功子集质量判定系统胜负。

### 5.3 模糊 Query 专项

Q01、Q03、Q06、Q07、Q08、Q19 各增加一个口语化改写，在 Keyword、Vector、Vector + Keyword、三通道四个条件下各运行 3 次。核心观察是标准 Query 与口语 Query 的 Hit@5、MRR 和延迟差值。

示例：标准问题“车辆肇事处理流程是什么？”改写为“公司车从申请到撞了以后该找谁、钱怎么处理？”两种问法保持同一事实目标和 Gold 来源。

### 5.4 多库路由专项

使用 Q09、Q12、Q13、Q15、Q16、Q17、Q18、Q19，对比自动目录路由、强制全部知识库和 Gold KB 限定。记录目标库召回、实际查询库数量、无关证据率、Hit@5 和延迟。该实验同时验证减少无关上下文与节省检索开销，不能只报告速度。

### 5.5 故障降级与知识缺口

使用 Q01、Q03、Q06、Q08、Q15、Q20，对比正常向量、关闭向量和注入 Embedding 错误。验收标准是请求不整体崩溃、Keyword/Graph 能继续返回证据、降级被显式记录。Q20 还必须拒绝编造“PBC 精确降低员工流失率”的百分比。

### 5.6 新鲜度与 Skill 回归

新鲜度实验复用 `multikb-vector-20260904` 的 F0–F3 结果，并为八道时间或冲突题补充人工 `stale_fact_error` 标签。只有在陈旧错误下降至少 25%、普通题质量下降不超过 2%、p95 延迟不超过 1.15 倍时，才支持晋级结论。

统一 Skill 回归运行 Fast-Q01、Balanced-Q06、Reasoning-Q08、Research-Q19、Research-Q20，检查实际通道、调用上限、分解与补检、充分性检查及拒答行为。

<!-- AUTO:PAIN_POINT_RESULTS_START -->

| 实验 | 对照 | 主要结果 | 可支持的结论 |
|---|---|---|---|
| 模糊 Query | 标准 / 口语 | Keyword Hit@5 66.7%→33.3%；Vector 100%→100%，MRR 100%→78.3%；Vector+Keyword 100%→50.0%；三通道 83.3%→66.7% | 向量单通道在这 6 题上最能保持召回，但模糊表达仍损伤排序；增加通道不保证更好 |
| 多库路由 | auto / all / gold | 三组 Hit@5 均为 87.5%、MRR 均为 45.8%；p95 分别 18.72/28.27/17.35 秒 | 自动路由保持本批召回并减少全库检索延迟；无关证据率均为 0，不能声称污染率下降 |
| 故障降级 | normal / disabled / error | 三组 Hit@5 均为 100%；MRR 58.3%→47.8%/47.8%；p95 14.42 秒→31.4/32.8 ms | 向量故障未中断请求，但排序质量下降；本地降级延迟不可解释为总体更优 |
| 新鲜度 | F0–F3 | F0/F1 Hit@5 87.5%，F2/F3 100%；陈旧事实错误率均为 62.5% | 新鲜度配置改善来源召回，但尚未改善确定性回答中的时间判断 |
| Skill 回归 | 四档 Profile | 13 个 Node 测试通过；5 个 Agent 运行项中 Fast-Q01 成功，其余 4 项两次尝试后仍失败 | Fast 的单次向量与调用上限已端到端验证；Balanced/Reasoning/Research 仍需在更稳定的 Agent 环境补验 |

<!-- AUTO:PAIN_POINT_RESULTS_END -->

## 6. 已有结果及复用边界

`multikb-vector-20260904` 包含 364 次真实语料运行，可复用 Mobilework 通道消融与新鲜度召回结果。已知结果为：Keyword Hit@5 80%、Vector 95%、Vector + Keyword 90%、三通道 90%；相应 p95 分别约为 0.032 秒、7.17 秒、5.20 秒和 15.72 秒。

这批实验的回答是确定性摘句，不是完整 Agent 回答；重复次数也不增加独立问题数量。第一条 Vector 请求存在异常长等待。因此这些数字可说明小样本上的召回与延迟权衡，不能直接证明最终回答质量或统计显著优势。

`multikb-local-20260904` 只用于无向量降级和本地延迟参考。旧 terminal profile 只作为改造前快照。两者均不得与当前分支结果混称为同一版本。

旧 nashsu 无向量结果已改名为 `nashsu_token_graph` 消融组：36 条检索的 Hit@5 为 50.0%、MRR 为 37.5%、p95 为 57.9 ms。它不再出现在主 Baseline 行；新的主行来自相同 OpenRouter 向量且每条 `vectorHits > 0` 的 36 条检索。

## 7. 结果解读原则

- 只有同一语料、同一 Query 和相同 Top-K 下的三方结果进入主对比表。
- 公开仓库自有语料上的成绩只作为背景，不参与胜负判断。
- 延迟受网络、远程 Embedding、冷/热缓存和原生接口影响，报告运行条件和失败分布。
- 小样本差异表述为“本批观测”，不写成统计显著提升。
- 向量关闭或故障后仍返回答案不代表质量不变，必须同时报告降级后的 Hit@5 和证据质量。
- 新鲜度重排提高来源召回不等于陈旧事实错误下降，后者必须依赖人工标签。

## 8. 面向导师的八页汇报结构

1. **问题背景**：单库固定检索在简单题、多库题、模糊题和时间冲突中的成本与风险。配一张 Query → 证据 → 回答流程图。
2. **Baseline 演进**：Karpathy 可运行范式、nashsu 工程化产品、Mobilework 多库 Agent 检索。配三方能力表。
3. **原有基础与本分支贡献**：区分已有三通道 + RRF 与新增多库路由、联邦检索、预算和统一 Skill。
4. **统一 Skill 架构**：主 Skill、Profile Reference、插件配置和后端守卫的职责边界。
5. **实验设计**：50 页同语料、20 题题库、六类痛点、真实失败保留策略。
6. **三方主结果**：展示 Hit@5、MRR、回答质量和质量—延迟散点图。
7. **痛点量化结果**：展示标准/口语 Query、路由前后、正常/故障和 F0–F3 对比。
8. **结论与边界**：列出已被数据支持的结论、仍不能证明的结论和下一步人工语义评测。

## 9. Excel 对照

最终工作簿固定包含八张表：`Summary`、`System Comparison`、`Pain Point Tests`、`Skill Regression`、`Raw Runs`、`Queries & Rubric`、`Sources & Setup`、`Failures`。汇总指标和图表由 `Raw Runs` 公式驱动。工作簿遇到缺失或失败数据时显示 `n.a.`，并在 Failures 保留原因。

最终工作簿已由 Microsoft Excel 全量重算：218 个公式、0 个公式错误；Artifact Tool 对八张工作表完成重新导入、错误扫描和渲染检查。`Failures` 保留全部失败尝试及 `superseded_by` 关系；汇总只统计 1,244 条当前有效记录，其中 46 条为当前有效失败。
