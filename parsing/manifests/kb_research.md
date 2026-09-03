# kb_research 来源与解析台账

获取日期：2026-09-04。六份 Markdown 均为独立事实摘要，不是未经核验的全文解析。下载时间不替代出版、修订或生效日期；所有空缺日期保留未知。网页均经工具读取与核对，未使用 Wikipedia。

| Markdown | 原始/获取记录 | 证据范围 | 状态 |
|---|---|---|---|
| 一般纳税人应纳税额如何计算.md | vat-2020.html | 2020 官方问答 | HTTP 下载成功 |
| 古树名木保护条例解读.md | ancient-tree-2025.html | 2025 官方网站解读 | HTTP 下载成功 |
| flexible-benefits-classic.md | flexible-benefits-classic.web-extract.md | 1992 出版社摘要 | 直接 HTTP 403；web 检索可读取出版社摘要 |
| flexible-benefits-research.md | flexible-benefits-research.html | 2016 机构仓储摘要及元数据 | HTTP 下载成功；2024 仅为入库日 |
| marine-industrial-agglomeration-2024.md | marine-2024.web-extract.md | 2024 出版社摘要 | 直接 HTTP 403；web 检索可读取出版社摘要 |
| large-yellow-croaker-agglomeration-2022.md | croaker-2022.html | 2022 期刊摘要 | HTTP 下载成功 |

原件目录：`parsing/resource/kb_research`。用于构建的摘要目录：`kb/kb_research/raw/sources`。每份摘要的 YAML 记录标题、URL、出版者、发表日期、访问日期、许可/访问状态、证据类型及 SHA-256。两个 web-extract 文件明确不是原始 HTML/PDF，哈希只验证本地获取记录。

本次未下载或声称阅读 PDF；四份原始 HTML 已保存。没有替换研究题目，受限的两篇降低为摘要级证据。HTML 原件及获取记录保留本地、不推送；可分发事实摘要和本台账纳入 Git。

## 评测注意事项

- 1992 研究证明的主要是满意度变化，不能伪造吸引/留任效果。
- 2016 研究使用 308 名 HR 经理的问卷，不应把报告的能力当作实测人员流失率。
- 海洋 2024 研究明确具有地区异质性，不能回答为所有地区正向。
- 2022 大黄鱼研究的数据期是 2011–2020，研究目标是集聚水平，不是因果效应。
- 古树材料若与企业原文一致，应记为一致或补充，不应制造冲突。
- 税务 2020 历史材料没有陈述现行退税排除规则，必须与当前法规分时引用。
