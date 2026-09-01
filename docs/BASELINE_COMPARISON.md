# mobilework 与 nashsu / green-dalii Baseline 对比报告

> 说明：本工程（mobilework）从参考实现 new-wiki 重构而来。本文的**本机性能数字（第 3 节）沿用参考实现 new-wiki 的历史实测**，尚未在 mobilework 上重跑；架构与算法描述（第 1、2、6 节的检索侧）已按 mobilework 当前实现更新。凡属参考实现的数字均显式标注，未在本工程上复现的不谎报为本工程跑分。

## 1. 结论摘要

三个系统都遵循 Raw Sources → Compiled Wiki → Schema，但优化目标不同：

- **mobilework（本项目）**：生命周期正确性最突出。稳定 source ID、精确 page↔source 血缘、staging、并发冲突拒绝、提交日志与可恢复删除构成完整安全链；检索侧已从早期「词法加权 + 1-hop 图扩展」升级为三通道（vector/keyword/graph）+ RRF 融合 + LanceDB 向量库的 MCP 服务，但大规模图算法仍是 MVP。
- **nashsu/llm_wiki**：完整桌面产品和图分析最成熟。两阶段摄取、持久队列、四信号相关性、Louvain 社区、Sigma/Graphology/ForceAtlas2 和可选向量检索适合较大知识图谱。
- **green-dalii/obsidian-llm-wiki**：Obsidian 内检索算法最强且有公开准确率。五阶段级联与 Monte-Carlo Personalized PageRank 不依赖 embedding，并公布 PPR@5 27.1%（其自有语料）。

不能诚实地给出"三个项目在本机的统一端到端秒数"：nashsu 是 Tauri/Rust 桌面应用，green 是 Obsidian 插件，且模型、Provider、并发、抽取粒度会支配总耗时。本报告将**参考实现实测**、**官方公开数据**和**架构推断**分开标记。

## 2. 算法对比

| 维度 | mobilework | nashsu | green-dalii |
|---|---|---|---|
| 交付形态 | Python + OpenCode Skill + MCP 检索服务 | Tauri v2 / Rust + React | Obsidian TypeScript 插件 |
| 增量身份 | SHA-256 + 稳定 UUID；移动唯一匹配 | 增量 cache、source watcher | Smart Batch Skip、内容 hash 去重 |
| 摄取 | 单个 Agent batch 中编译/合并页面 | 两阶段 CoT：分析后生成 | 可调粒度实体/概念抽取；页面并发 3–5 |
| 删除 | 独占页归档；共享页按幸存来源语义重写 | watcher 同步 delete cleanup | watcher + maintenance；保护 reviewed 页面 |
| 写入安全 | staging → validate → commit；并发 hash；journal 回滚 | 持久队列、crash recovery、cancel/retry | allSettled、单页重试、取消与部分结果保留 |
| 图谱节点 | 编译 Wiki 页面 | Wiki 页面 | Obsidian Wiki 页面 |
| 图谱边 | Wikilink + 共享来源 + 共同邻居（keyword/graph 通道） | 同类四信号模型 | Wikilink；检索阶段运行 PPR |
| 检索通道 | vector（LanceDB）+ keyword（词法打分）+ graph（1-hop 扩展），RRF 融合 | tokenized + graph relevance + 可选 LanceDB | Lex → LLM关键词 → 本地扫描 → LLM fallback → PPR |
| 检索工具面 | 3 个 MCP 工具：retrieve / graph_neighbors / knowledge_tree | 桌面应用内建 | Obsidian 命令 |
| 公开检索准确率 | 尚无 | 无同口径公开值 | PPR@5 27.1%，纯 kNN 24.1%（作者语料） |
| Embedding | 可选（provider-agnostic；默认 qwen3-embedding-8b，可切本地部署）；缺失时向量通道降级、其余通道照常 | 可选 | 无 |

### 算法复杂度判断

- mobilework 来源扫描：`O(S)`，S 为来源数；每次计算文件 hash。
- mobilework 当前图谱：页面两两检查共享来源和共同邻居，约 `O(P²)`；graph 通道扩展同样受 `O(P²)` 影响。keyword 通道为词法打分，随语料规模线性。
- nashsu：图谱建立后使用 Graphology/ForceAtlas2；四信号和 Louvain 成本取决于图规模，具有专门图数据结构和布局缓存，工程扩展性优于当前 mobilework。
- green：词法扫描与页面数相关；PPR 使用 3,000 条随机游走 × 50 步，官方描述该扩展阶段成本 `O(K×L)`，与页面总数基本解耦，但前置候选扫描仍与 vault 大小相关。

## 3. 本机性能实测（参考实现 new-wiki，尚未在 mobilework 重跑）

环境：参考实现的 Windows 主机、Python 3.11；每项预热一次后重复采样，表中为中位数；使用 `tracemalloc`，因此绝对时间包含测量开销。合成页面每页2个 Wikilink、每2页共享一个来源。**以下数字为参考实现历史结果，仅作量级参考，不代表 mobilework 本机跑分。**

| 来源/页面数 | 无变化扫描 | 单文件修改 | 单文件删除 | 全量图谱构建 | 查询候选检索 |
|---:|---:|---:|---:|---:|---:|
| 100 | 101.25 ms | 102.66 ms | 102.48 ms | 269.33 ms | 404.60 ms |
| 300 | 308.09 ms | 305.62 ms | 303.37 ms | 1,809.40 ms | 2,240.62 ms |
| 500 | 510.92 ms | 501.25 ms | 503.80 ms | 4,742.85 ms | 5,430.66 ms |

观察：来源扫描近似线性，约 1.0 ms/来源（含 tracemalloc）；图谱从100到500页增长约17.6倍，符合二次复杂度趋势。500页时已不适合在每次查询中同步重建图谱——mobilework 检索侧因此改用 LanceDB 持久向量索引 + corpus 单例缓存（mtime 失效），避免每次查询重建。

### 历史端到端观测（参考实现）

根据 batch ID 创建时间与 `wiki/log.md` 提交时间计算（包含解析、OpenCode、模型网络和提交，不是严格受控实验）：

- 5份PDF → 10个页面：约 **200 s**，约40 s/来源。
- 1份PDF → 2个页面：约 **54 s**。
- 删除1个来源并归档2页：约 **30 s**。

这些数字说明端到端主要由模型调用支配，不能与使用不同模型/Provider的 baseline 秒数直接比较。

## 4. 质量/能力评分（5分制，基于可验证功能，不是统一语料准确率）

| 维度 | mobilework | nashsu | green |
|---|---:|---:|---:|
| 来源增删改正确性 | **5** | 4 | 3.5 |
| 事务与并发保护 | **5** | 4 | 3.5 |
| 图谱分析成熟度 | 2.5 | **5** | 3.5 |
| 查询检索算法 | 3.5 | 4 | **5** |
| 大规模图性能 | 2 | **4.5** | 4 |
| 部署轻量性 | 4 | 3 | **5**（已有Obsidian时） |
| 独立检索服务 | 4 | **5** | 2（依赖Obsidian） |

评分依据：mobilework 的生命周期能力由本地生命周期/增量测试验证；检索侧新增 vector/keyword/graph 三通道 + RRF 融合，较早期纯词法方案提升（查询检索算法 2.5→3.5），但尚无统一语料准确率。baseline 分数来自其公开功能与架构说明，不代表同一测试集上的数值准确率。

## 5. 公平三方端到端实验应如何做

1. 固定同一台机器、同一模型、同一Provider、temperature、网络和并发数。
2. 准备20份公开PDF：5个主题，每主题4份；记录页数和文本字符数。
3. 统一抽取粒度：每来源最多1个source页、5个concept/entity页。
4. 每个系统使用全新空 vault，冷启动3次、热启动5次。
5. 分段计时：检测、解析、模型、落盘/提交、图谱更新、查询。
6. 运行事件集：20新增、1修改、纯重命名、1删除独占、1删除共享来源、无变化扫描。
7. 查询集至少30题：事实10、关系10、多跳5、知识缺口5。
8. 人工盲评：事实正确率、Citation Hit@5、来源覆盖率、过时claim残留率、重复页面率、死链率。

建议主指标：

- `T_ingest_p50/p95`、`T_update_p50/p95`、`T_query_first_token/p50`
- Citation Hit@5、Answer groundedness、Page dedup precision
- 删除后 stale-claim rate（越低越好）
- crash recovery success、manual interventions / 100 events
- 总输入/输出 tokens 与API成本

## 6. 最终判断与优化优先级

如果论文/汇报重点是"原始资料变化后能否安全自动维护"，mobilework 的差异化最强；如果重点是"大图分析"，nashsu 更成熟；如果重点是"无embedding的图感知查询准确率"，green 当前证据最强。

mobilework 检索侧现状与下一步：

1. **已落地**：三通道检索（vector/keyword/graph）+ RRF 融合；LanceDB 持久向量索引 + `index_meta.json` 新鲜度判定；corpus 单例缓存（mtime 失效）避免每次查询重建图谱；向量通道 embedding 失败时降级、其余通道照常。
2. 用 `source_id → pages` 倒排表替代所有页面两两比较，把共享来源边从 `O(P²)` 降到按桶生成。
3. 缓存 adjacency 和页面前置信息，仅对变化页面增量更新。
4. keyword 通道引入 BM25 或倒排索引，再做1–3 hop图扩展。
5. 增加 PPR 对照组和固定查询集，报告 Hit@5/MRR，而不只比较功能。
6. 对来源解析和独立页面生成增加受控并行；最终 commit 仍保持串行事务。

## 7. 可复现文件

- `tests/test_lifecycle.py` / `tests/test_incremental.py`：构建侧生命周期与增量测试。
- `tests/test_retrieval.py`：检索侧三通道 / 融合 / 降级测试。

评测组的确定性阶段基准脚本与原始结果 JSON（对应参考实现的 `benchmarks/benchmark_local.py` 与结果文件）在 mobilework 中尚未落地——`benchmarks/` 归评测组维护，本次重构不改动，当前仅有占位 `.gitkeep`。

检索侧手动验证（有 embedding key 时）：

```bash
cd /Users/jialulu/projects/mobile-wiki/mobilework
.venv/bin/python -m wiki_retrieval.index --limit 5      # 产出 .lancedb/index_meta.json
.venv/bin/pytest tests -q                                # 生命周期 + 增量 + 检索测试
```

## 8. 证据边界

- 第 3 节本机数字来自参考实现 new-wiki 的历史实测，未在 mobilework 上重跑，仅作量级参考。
- 本机无法稳定下载两个公开仓库（GitHub连接被重置），因此没有伪造 baseline 本机耗时。
- nashsu和green的算法、技术栈、并发建议及公开准确率来自各自官方README/Discussion。
- green的PPR@5结果来自其自有语料，不能当作三方统一benchmark结论。
- "Rust/Graphology预期更快""检索通道升级带来准确率提升"等属于架构推断或功能层判断，必须在统一实验完成后才能转化为实测结论。
