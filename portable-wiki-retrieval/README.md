# Portable Wiki Retrieval

Portable Wiki Retrieval 是一个可独立复制的本地 Markdown Wiki 检索工具包，包含：

- 一个通过 `stdio` 运行的完整 MCP 服务；
- 一个简体中文 `wiki-retrieval` Skill；
- OpenCode、Codex、Claude Code 的自动安装与配置；
- Markdown 解析、增量索引、SQLite FTS5 关键词检索、LanceDB 向量检索、实体与链接关系检索；
- Query 规范化、意图判断、有限多路改写、Wiki 路由、RRF 融合、可选 reranker、证据裁剪和降级状态。

它接受任意嵌套的 Markdown 文件树，不要求固定目录结构，不修改 Wiki 原文件，也不依赖原 Mobilework 项目。

## 一、环境要求

- Python 3.11 或更高版本；
- 安装时需要联网；
- Python 自带的 SQLite 必须支持 FTS5；
- 一个 OpenRouter API Key；
- OpenCode、Codex、Claude Code 至少安装一个。

外部模型默认统一通过 OpenRouter 使用：

- Embedding：`qwen/qwen3-embedding-8b`
- Reranker：`cohere/rerank-4-pro`

MCP 本身不会调用聊天模型。没有可用向量时，关键词检索仍能工作，并会在返回结果中说明向量通道已降级。

## 二、最快安装方式

先把整个文件夹复制到目标电脑，不要只复制 `src` 或 `skills`。

### Windows PowerShell

在本文件夹中运行：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\install.ps1 -Client opencode -Project "C:\你的\OpenCode项目"
```

把 `opencode` 换成 `codex`、`claude-code` 或 `all`，即可安装到对应客户端。`-Project` 是日常打开客户端工作的项目目录，不是 Wiki 目录。

### macOS / Linux

```sh
sh install.sh opencode /path/to/your/opencode-project
```

安装器会：

1. 检查 Python 版本和 SQLite FTS5；
2. 在本文件夹创建独立 `.venv`；
3. 安装锁定依赖和 MCP；
4. 从样例生成本机 `config.toml` 与 `.env`，不覆盖已有文件；
5. 将 Skill 和 MCP 配置写入指定客户端项目。

安装完成后，打开本文件夹中的 `.env`，只填写：

```dotenv
OPENROUTER_API_KEY=你的_OpenRouter_API_Key
```

不要把填写后的 `.env` 发给别人或提交到 Git。

## 三、确认安装成功

Windows：

```powershell
.\.venv\Scripts\portable-wiki.exe --config .\config.toml doctor
.\.venv\Scripts\portable-wiki.exe --config .\config.toml config validate
```

macOS / Linux：

```sh
./.venv/bin/portable-wiki --config ./config.toml doctor
./.venv/bin/portable-wiki --config ./config.toml config validate
```

OpenCode 可进一步检查：

```powershell
opencode debug skill
opencode mcp list
```

应能看到 `wiki-retrieval`，并且 `portable-wiki` MCP 为 connected。安装或配置变更后，需要完全退出并重新启动客户端。

## 四、注册 Markdown Wiki

注册前建议先做只读检查。以下以 Windows 为例：

```powershell
$WikiTool = ".\.venv\Scripts\portable-wiki.exe"

& $WikiTool --config .\config.toml wiki inspect "C:\资料\company-wiki" --wiki-id company
& $WikiTool --config .\config.toml wiki register company "C:\资料\company-wiki" --name "公司知识库"
& $WikiTool --config .\config.toml wiki list
```

`wiki_id` 建议使用稳定的英文小写名称，例如 `company`、`research`、`project-a`。一个 Wiki 对应一个独立 SQLite 索引和 LanceDB 向量库。

其他维护命令：

```powershell
# Wiki 文件变化后增量更新索引
& $WikiTool --config .\config.toml wiki reindex company

# 修改显示名称或根目录
& $WikiTool --config .\config.toml wiki update company --name "新名称"
& $WikiTool --config .\config.toml wiki update company --root "D:\新的\company-wiki"

# 注销并删除派生索引；不会删除 Wiki 原文件
& $WikiTool --config .\config.toml wiki unregister company
```

注册时默认读取任意层级的 `**/*.md`，并排除 Git 和工具状态目录。标题、heading、frontmatter、别名、实体及 Markdown 链接会进入索引。

## 五、在 OpenCode、Codex 或 Claude Code 中使用

Skill 会根据用户是否在询问已注册知识库自动触发，通常不必手动输入 `/` 或明确说“使用某个 Skill”。直接提问即可，例如：

```text
只检索 company：公司的试用期规定是什么？请引用具体文件和 heading。
```

```text
只检索 company：现行远程办公制度是什么？不要比较历史版本。
```

```text
只检索 company：比较 2020 年规定和后来新制度的差异，把新旧依据分开。
```

```text
从 research 中查一下这个概念与哪些页面有关，并说明证据链。
```

如果需要严格指定流程，也可以说：

```text
使用 wiki-retrieval，以 reasoning 档位只检索 company，回答……；不要直接读取 Wiki 文件。
```

回答中的事实应来自 MCP 返回的 Evidence，并保留 Wiki ID、相对路径和 heading。Skill 不会自动扫描电脑寻找未知目录，也不会在未获明确授权时注册 Wiki。

## 六、检索档位

- `fast`：单主分支，关键词和向量召回，不执行 reranker；适合简单事实。
- `balanced`：默认档位，允许有限辅助分支和自动 reranker；适合一般问答。
- `reasoning`：允许更多子问题、实体和一层关系扩展；适合需要综合解释的问题。
- `research`：预算最高，允许更深关系扩展和语义实体扩展；适合系统研究。

普通 Query 默认只有一条主分支。关键词和向量并行属于同一分支；只有时间对照、比较、关系、来源或清单等明确需求才按需增加分支。

## 七、配置

完整样例位于 `config.example.toml`。首次安装会复制为 `config.toml`，所有运行参数以该文件为准，包括：

- embedding 和 reranker 模型、批大小及超时；
- chunk 大小、overlap、heading 合并及增量刷新；
- LanceDB 距离类型、ANN 索引阈值与分区大小；
- BM25、向量、实体、图通道的融合权重；
- 各检索档位的时间、分支、子问题和证据预算；
- 实体扩展阈值与降级策略。

常用配置命令：

```powershell
& $WikiTool --config .\config.toml config show
& $WikiTool --config .\config.toml config get profiles.balanced.max_query_branches
& $WikiTool --config .\config.toml config set profiles.balanced.max_query_branches 2
& $WikiTool --config .\config.toml config unset profiles.balanced.max_query_branches
& $WikiTool --config .\config.toml config validate
```

修改 embedding 模型、向量维度或 chunk 策略后，已有索引会被标记为 stale，需要重新执行 `wiki reindex`。

## 八、MCP 工具

服务入口为 `portable-wiki-mcp` 或：

```powershell
.\.venv\Scripts\python.exe -m portable_wiki_retrieval.server
```

提供以下工具：

- `retrieve`：完成 Query 规划、多通道召回、融合、可选 rerank 和 Evidence 返回；
- `prepare_query`：仅预览 Query 计划；
- `graph_neighbors`、`knowledge_tree`：关系扩展与知识树；
- `list_wikis`、`inspect_wiki`、`register_wiki`、`update_wiki`、`unregister_wiki`：Wiki 管理；
- `retrieval_capabilities`：查看能力及降级状态；
- `get_settings`、`update_settings`、`reset_settings`：配置管理。

MCP、CLI 和 Skill 使用同一套核心实现。运行状态默认位于系统用户状态目录；确切路径可通过 `portable-wiki doctor` 查看。

## 九、常见问题

### OpenCode 显示 No MCP servers configured

在实际运行 OpenCode 的项目目录重新执行安装，并重启 OpenCode：

```powershell
.\install.ps1 -Client opencode -Project "C:\实际项目目录"
```

### 索引显示 partial 或 embedding degraded

关键词索引已经可用，但部分向量没有生成。检查 `.env` 中的 Key 和网络后重新执行：

```powershell
& $WikiTool --config .\config.toml wiki reindex company
```

索引器会复用内容指纹一致的已有向量，只补齐缺失或变化的分块。

### OpenRouter 返回 401

Key 无效、已撤销或客户端仍在使用旧环境。更新 `.env` 后完全重启客户端。错误信息会脱敏，不应打印完整 Key。

### 没有向量还能否检索

可以。SQLite FTS5 的关键词/BM25 检索独立存在；向量服务不可用时，`auto` 模式会降级并在响应的 `degradations` 和模型状态中说明。语义召回质量会下降，但基础检索不会完全失效。

## 十、目录结构

```text
portable-wiki-retrieval/
├── skills/wiki-retrieval/       # 中文 Skill
├── src/portable_wiki_retrieval/ # 完整 MCP、CLI 和检索实现
├── scripts/install.py           # 跨客户端安装逻辑
├── config.example.toml          # 全量参数样例
├── .env.example                 # OpenRouter Key 样例
├── .gitignore                   # 防止提交密钥、配置和运行索引
├── install.ps1                  # Windows 快速安装
├── install.sh                   # macOS/Linux 快速安装
├── pyproject.toml               # Python 包与命令入口
├── requirements.lock            # 锁定依赖
├── LICENSE                      # Apache-2.0
└── README.md                    # 本操作手册
```

## 十一、向量化与 LanceDB 代码位置

向量化、落库和检索的完整链路都在 `src/portable_wiki_retrieval` 中：

1. `openrouter.py` 的 `OpenRouterClient.embed()`：将文档分块或查询发送到 OpenRouter embedding 接口，指定 `search_document` 或 `search_query`，校验返回数量、维度和有限数值，并返回浮点向量。
2. `indexer.py` 的 `build_index()`：解析 Markdown 后调用 `client.embed()` 批量生成文档向量；使用 `chunk_id + sha256` 判断哪些旧向量可以复用，只向量化新增或变化的分块。
3. `indexer.py` 的 `_normalize_vector()`：将向量转换为 NumPy `float32` 并做单位归一化，拒绝空向量、非有限值和零范数。
4. `indexer.py` 的 `build_index()`：连接临时目录 `vectors.lancedb.tmp`，创建 LanceDB 的 `chunks` 表，写入 `chunk_id`、内容指纹 `sha256` 和 `vector`；数据量达到配置阈值时创建 ANN 索引。写入成功后原子替换正式的 `vectors.lancedb`，避免半成品覆盖旧索引。
5. `indexer.py` 的 `vector_database_path()`：决定每个 Wiki 的实际向量库位置，即 `<state_dir>/indexes/<wiki_id>/vectors.lancedb`。同目录的 `index.sqlite3` 保存文档、分块、FTS5、实体、链接及向量元数据。
6. `indexer.py` 的 `IndexStore.vector_search()`：打开 LanceDB `chunks` 表，把 Query 向量归一化后按 cosine 距离检索，并将距离转换为相似度分数。
7. `service.py` 的 `RetrievalService.retrieve()`：为实际 Query 分支生成查询向量，并行执行 keyword/vector/entity/graph 召回，随后去重、加权 RRF 融合和可选 reranker，最终返回 Evidence、来源、Trace 与降级状态。
