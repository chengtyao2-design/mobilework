# Mobilework

Mobilework 是一个由 OpenCode 驱动的本地 Wiki 对话、增量构建与分级检索工具。要求 Python 3.11+ 和 OpenCode 1.18.25+。

多知识库模式使用 `kb/<kb_id>` 独立存储。`/retrieve` 选择 Fast / Balanced / Reasoning / Research，`/retrieve-options` 调整细化开关。配置、预算与 Skill/后端职责见 [多库检索开关](docs/multi-kb-retrieval-controls.md)。

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

默认使用 Qwen 3.8 Flash。需要以 Qwen 3.8 Max 启动复杂任务会话时运行：

```powershell
.\mobilework --model-profile primary
```

进入界面后也可以使用 OpenCode 原生 `/models` 切换模型；Mobilework 不会因检索等级变化覆盖用户的模型选择。

## 对话式工作流

- 直接输入问题：使用当前检索等级回答。
- `/retrieve`：选择 `Fast / Balanced / Reasoning / Research`，默认 `Balanced`。设置保存在 `.mobilework-state/preferences.json`；切换档位重置高级覆盖，`/retrieve-options` 可独立调整能力。
- `/sync`：只读扫描资料变化，显示新增、修改、移动、恢复、删除和 pending 信息；确认后在当前会话中启动一次 `wiki-builder`。
- `@wiki/...`：引用已生成 Wiki。
- `@sources/...`：引用原始资料。
- `@docs/...`：引用项目文档。

界面、加载动画、thinking 摘要、工具调用折叠、Toast、确认框和会话恢复都由 OpenCode 原生 TUI 提供。Mobilework 只展示可理解的思考摘要，不承诺显示模型的私有完整推理链。

### 检索等级

| 等级 | 策略 |
| --- | --- |
| Fast | 5 秒预算，最多一次纯向量检索 |
| Balanced | 12 秒预算，最多一次向量与关键词融合检索（默认） |
| Reasoning | 30 秒预算，最多四次检索，最多三个子问题 |
| Research | 90 秒预算，最多八次检索，包含充分性检查 |

所有档位加载公共规划和统一多库执行规则；是否分解、补检或检查证据由独立开关决定。后端负责目录路由和并行检索，Skill 不为每个库复制流程。任何档位都不会静默升级。普通界面只显示自然语言状态；内部 Skill、工具、路由参数和资料 ID 不进入回答正文。

## Wiki 同步与恢复

日常同步入口只有 TUI 内的 `/sync`。旧的 `wiki sync` 和 `wiki watch` 不再执行任务，只返回迁移提示，从而避免嵌套启动 `opencode run`。

底层事务命令仍保留给 `wiki-builder`、测试和故障恢复使用：`prepare`、`pending`、`record-source`、`checkout`、`rewrite`、`commit`、`abort`、`restore`、`doctor` 以及索引更新。需要诊断时可以运行：

```powershell
.\.venv\Scripts\wiki.exe --root . doctor
.\.venv\Scripts\wiki.exe --root . pending
```

`/sync` 取消时不会创建 batch 或调用模型。构建失败会保留 pending；页面提交成功但索引失败会明确显示为“提交成功、索引警告”。

## 配置与安全

模型在 `wiki.config.json` 的 `assistant.models` 中分三档配置，值的格式都是 `provider/model`：

- `default`：默认对话模型，当前为 Qwen 3.8 Flash。
- `primary`：可选主力模型，当前为 Qwen 3.8 Max。
- `small`：标题、短摘要等后台任务，当前为成本更低的 Qwen 3.7 Flash，不参与知识库最终事实回答。

模型凭据推荐通过 `opencode auth login` 保存。启动时生成的独立 OpenCode 配置只启用这些模型所属的 Provider，并只注册 Mobilework 的 Wiki MCP；主 Agent 禁止系统级 Skill、外部检索 MCP 和联网搜索介入检索调度。`wiki-builder` 则只允许加载维护 Skill。

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
