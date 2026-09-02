# Mobilework

Mobilework 是一个由 OpenCode 驱动的本地 Wiki 对话、增量构建与分级检索工具。要求 Python 3.11+ 和 OpenCode 1.18.25+。

## Windows 安装与启动

在项目根目录打开 PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev,parsing]"
npm install -g opencode-ai
opencode auth login
Copy-Item .env.example .env  # 已有 .env 时不要覆盖
.\mobilework
```

也可以在已激活虚拟环境后运行：

```powershell
mobilework
```

启动器在 Windows 上优先调用 `opencode.cmd`，因此不受 `opencode.ps1` 执行策略限制。它会检查项目虚拟环境、OpenCode 版本、模型配置、Provider 凭据和 Wiki/MCP 布局；只检查、不进入界面可运行：

```powershell
.\mobilework --check
```

每次 `mobilework` 都新建会话。进入 TUI 后使用 OpenCode 原生 `/sessions` 恢复历史会话。

## 对话式工作流

- 直接输入问题：使用当前检索等级回答。
- `/retrieve`：打开二级选择菜单，选择 `Naive / Low / Medium / High`。默认是 `Low`，选择保存在 `.wiki-state/preferences.json`，重启后仍有效。
- `/sync`：只读扫描资料变化，显示新增、修改、移动、恢复、删除和 pending 信息；确认后在当前会话中启动一次 `wiki-builder`。
- `@wiki/...`：引用已生成 Wiki。
- `@sources/...`：引用原始资料。
- `@docs/...`：引用项目文档。

界面、加载动画、thinking 摘要、工具调用折叠、Toast、确认框和会话恢复都由 OpenCode 原生 TUI 提供。Mobilework 只展示可理解的思考摘要，不承诺显示模型的私有完整推理链。

### 检索等级

| 等级 | 策略 |
| --- | --- |
| Naive | 单次纯向量检索 |
| Low | 单次向量与关键词融合检索（默认） |
| Medium | 自动加载公共检索规划 Skill，最多两轮补缺检索 |
| High | 自动加载公共检索规划 Skill，最多五轮在 retrieve、graph_neighbors、knowledge_tree 间自适应切换 |

Medium 和 High 会先加载 `wiki-retrieval-planner`，再加载对应分级 Skill。Planner 只负责查询改写、问题拆分、工具/scope/channel 路由以及证据停止条件，不调用工具，也不生成最终答案。任何等级都不会静默升级。

## Wiki 同步与恢复

日常同步入口只有 TUI 内的 `/sync`。旧的 `wiki sync` 和 `wiki watch` 不再执行任务，只返回迁移提示，从而避免嵌套启动 `opencode run`。

底层事务命令仍保留给 `wiki-builder`、测试和故障恢复使用：`prepare`、`pending`、`record-source`、`checkout`、`rewrite`、`commit`、`abort`、`restore`、`doctor` 以及索引更新。需要诊断时可以运行：

```powershell
.\.venv\Scripts\wiki.exe --root . doctor
.\.venv\Scripts\wiki.exe --root . pending
```

`/sync` 取消时不会创建 batch 或调用模型。构建失败会保留 pending；页面提交成功但索引失败会明确显示为“提交成功、索引警告”。

## 配置与安全

对话模型在 `wiki.config.json` 的 `assistant.model` 中配置，格式为 `provider/model`。模型凭据推荐通过 `opencode auth login` 保存。

向量检索读取 `.env` 中的 `EMBEDDING_API_KEY`。`EMBEDDING_BASE_URL` 和 `EMBEDDING_MODEL` 留空时使用代码默认值；没有 embedding key 时关键词和知识图谱通道仍可工作，向量通道会标记为降级或禁用。

以下内容由 `.gitignore` 排除，不应提交：

- `.env`、`opencode.json`
- `.wiki-state/`、`.wiki-trash/`、`.lancedb/`
- `raw/sources/*.md`
- `wiki/**/*.md`

OpenCode 读取权限还会阻止 Agent 访问 `.env`、`.git`、`.wiki-state` 和 `.wiki-trash`。原始资料和生成页面虽然不提交 Git，仍可以通过受控的 `@sources`、`@wiki` 引用。

## 开发测试

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

MCP 服务通常由启动器自动拉起；单独调试时可运行 `python -m wiki_retrieval.server`。完整检索评测需要设置 `MOBILEWORK_EVAL_CORPUS` 指向包含测试证据页的独立语料库。
