# 多库评测复现与证据边界

## 已构建语料

企业库保留 12 个活跃来源、40 个受管页面；研究库由 6 个公开来源摘要生成 10 个受管页面。研究库按两阶段分析、暂存、溯源记录和事务提交构建，未把超时的 OpenCode 调用记为构建成功。原文获取状态及 SHA-256 见 `parsing/manifests/kb_research.md`。

向量索引分别为 155 和 16 个 Chunk，模型 `qwen/qwen3-embedding-8b`，维度 4096。研究摘要不是论文全文，发布日期与下载日期、仓储入库日期分别记录。

## 运行

在应用根目录执行：

```powershell
.venv/Scripts/python.exe -m wiki_retrieval.index --project kb/kb_enterprise
.venv/Scripts/python.exe -m wiki_retrieval.index --project kb/kb_research
.venv/Scripts/python.exe -m benchmarks.retrieval_eval.run_multikb --output-dir benchmarks/retrieval_eval/artifacts/<unique-run-id>
```

仅本地检索可加 `--no-vector`，此时任何含向量的组合必须标为降级，不可用于宣称向量收益。远程索引构建会发送源文本，运行前应确认服务与资料外发权限。已注册 KB 自动加载应用 `.env`，不把任意父目录凭据传给未注册目录。

实验先跑 9×20 次，前三组各补 40 次；新鲜度 4×8 次，前两组各补 16 次，总计 364 次。每次完成即保存 JSONL，输出目录不可覆盖既有实验。模型、配置、Git commit、延迟、候选证据和抽取式回答保存在 `report.json` 与 JSONL 中。

## 当前评分限制

- 检索是真实语料和真实服务调用；回答为确定性摘句，不是 Agent 生成回答。
- 事实得分是标准化字符串匹配的代理值；语义等价表述可能被漏计。证据支撑分高不能证明问答推理正确，因为回答本身直接摘自证据。
- 来源召回按题库给定路径匹配；Raw 与 Wiki 的源文档等价映射尚未计入，因此不能据此公平比较不同 scope 的最终质量。
- 问题分解是表面连词拆分，充分性检查是页面数和字符数启发式，不代表模型规划或事实核验。
- 新鲜度的排序和上下文机制已执行，但摘句器不理解日期上下文。陈旧事实错误率未标注，Excel 留空，不将占位零值解释为无错误。
- 计时包含网络重试，超时在工具调用之间检查，不能取消已发出的远程请求。重复运行可能使用热语料缓存。
- 本批实验不自动提高任何功能的默认档位。需要补充经人工核验的语义评分、时间冲突标签、普通题回归和完整 Agent 回答实验后，才能按原门槛晋级。

## Excel

`build_multikb_workbook.mjs` 从 `report.json` 生成九张指定工作表。质量权重、聚合均值和最近秩 p95 使用可追溯公式；原始评分、候选与失败日志保持独立。工作簿及详细日志留存在本地 `benchmarks/retrieval_eval/artifacts/`，不自动提交其中的企业证据正文。

## 尚待完善的能力

后端已具备目录路由、联邦并行检索、RRF、来源隔离和 Claim 元数据。高级开关中问题分解、补检、跨库实体扩展及回答写回依赖 Agent/Skill 遵循流程；通用学习型 reranker、服务端硬截止取消与全部预算的统一强制执行仍需后续实现，不能把配置项存在等同于这些能力已完成。
