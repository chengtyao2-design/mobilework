# 文档解析接入

`extract.py` 是 mobilework 与朱恩铄解析服务之间的薄适配入口。解析领域、路由、质量门和多模态归一化继续由独立 `document_parser` 仓库维护；本目录不复制其实现。

```text
parsing/originals → document_parser → QualityPackage gate
→ MobileworkDeliveryAdapter → raw/sources + raw/assets + raw/metadata
```

在 mobilework 根目录运行：

```powershell
python parsing/extract.py --document-parser-root C:\path\to\document_parser
```

也可以设置 `$env:DOCUMENT_PARSER_ROOT` 后直接运行。固定解析器时增加 `--parser mineru`，解析器参数通过 `--options-json` 传入。

返回码：`0` 表示成功，`1` 表示至少一个 Source 解析失败，`2` 表示配置或接入失败。运行产物受 `.gitignore` 管理，不提交 Git。解析组拥有 originals 到质量通过数据包的过程；Wiki 组只读取 `raw/sources/*.md`。
