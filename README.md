# mobilework

一个本地 Wiki 构建、增量维护与检索项目。要求 Python 3.11+。

## Windows 快速启动

在项目根目录打开 PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev,parsing]"
Copy-Item .env.example .env  # 已有 .env 时不要覆盖
.\.venv\Scripts\wiki.exe --root . init
.\.venv\Scripts\wiki.exe --root . doctor
```

当前工作区已经完成以上环境安装和初始化，可以直接运行：

```powershell
.\.venv\Scripts\wiki.exe --root . status
.\.venv\Scripts\wiki.exe --root . prepare
.\.venv\Scripts\python.exe -m pytest -q
```

`prepare` 会为 `raw/sources/` 中的新资料创建待处理批次。查看工作单：

```powershell
.\.venv\Scripts\wiki.exe --root . pending
```

## 运行模式

- `wiki status`：查看新建、修改、删除的资料及待处理批次。
- `wiki prepare`：创建 staging 工作单，不调用 agent。
- `wiki sync --no-agent`：执行一次扫描并创建工作单。
- `wiki watch --no-agent`：持续监控资料变化，但不调用 agent。
- `wiki sync` / `wiki watch`：调用 `wiki.config.json` 中配置的 OpenCode agent；需要另行安装 `opencode` 和对应的 `wiki-maintainer` skill。
- `python -m wiki_retrieval.server`：以 stdio 启动 MCP 检索服务，通常由 MCP 客户端拉起，不是浏览器 Web 服务。

向量检索读取 `.env` 中的 `EMBEDDING_API_KEY`。`EMBEDDING_BASE_URL` 和 `EMBEDDING_MODEL` 留空时会使用代码内默认值；没有 API key 时仍可使用关键词和知识图谱检索。

## MCP 配置

`opencode.example.json` 使用 Unix/macOS 虚拟环境路径。在 Windows 上复制为 `opencode.json` 后，将命令改为：

```json
"command": [".venv/Scripts/python.exe", "-m", "wiki_retrieval.server"]
```

## 检索评测

完整评测需要一套独立语料库，其中应包含 `questions.v1.json` 声明的证据页面：

```powershell
$env:MOBILEWORK_EVAL_CORPUS = "C:\path\to\demo-data"
.\.venv\Scripts\python.exe benchmarks\retrieval_eval\run.py --corpus $env:MOBILEWORK_EVAL_CORPUS
```

未安装该专用语料库时，相关集成测试会跳过；普通单元测试仍会运行。
