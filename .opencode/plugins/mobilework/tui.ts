import { readFileSync } from "node:fs"
import { mkdir, rename, writeFile } from "node:fs/promises"
import { dirname, join } from "node:path"
import type { TuiDialogStack, TuiPlugin } from "@opencode-ai/plugin/tui"

type Tier = "naive" | "low" | "medium" | "high"
type Preview = {
  pending_batch?: string | null
  pending_count?: number
  change_total: number
  change_counts: Record<string, number>
}

const TIERS: Array<{ title: string; value: Tier; description: string }> = [
  { title: "Naive", value: "naive", description: "一次纯向量检索，速度最快" },
  { title: "Low", value: "low", description: "一次向量 + 关键词融合（默认）" },
  { title: "Medium", value: "medium", description: "先规划，再执行最多两轮检索" },
  { title: "High", value: "high", description: "先规划，再执行最多五轮自适应检索" },
]

function rootOf(): string {
  return process.env.MOBILEWORK_ROOT || process.cwd()
}

function preferencePath(root: string): string {
  return join(root, ".wiki-state", "preferences.json")
}

function readTier(root: string): Tier {
  try {
    const parsed = JSON.parse(readFileSync(preferencePath(root), "utf8"))
    return TIERS.some((item) => item.value === parsed?.retrieval_tier) ? parsed.retrieval_tier : "low"
  } catch {
    return "low"
  }
}

async function writeTier(root: string, tier: Tier): Promise<void> {
  const target = preferencePath(root)
  const temporary = `${target}.tmp`
  await mkdir(dirname(target), { recursive: true })
  await writeFile(temporary, `${JSON.stringify({
    version: 2,
    retrieval_tier: tier,
    revision: Date.now(),
  }, null, 2)}\n`, "utf8")
  await rename(temporary, target)
}

async function runWiki(root: string, args: string[]): Promise<any> {
  const python = process.env.MOBILEWORK_PYTHON || join(
    root,
    ".venv",
    process.platform === "win32" ? "Scripts/python.exe" : "bin/python",
  )
  const bun = (globalThis as any).Bun
  if (!bun?.spawn) throw new Error("当前 OpenCode 运行时不支持本地进程调用")
  const child = bun.spawn([python, "-m", "wiki_maintainer", "--root", root, ...args], {
    cwd: root,
    env: process.env,
    stdout: "pipe",
    stderr: "pipe",
  })
  const [stdout, stderr, code] = await Promise.all([
    new Response(child.stdout).text(),
    new Response(child.stderr).text(),
    child.exited,
  ])
  if (code !== 0) throw new Error(stderr.trim() || stdout.trim() || `wiki exited with ${code}`)
  return JSON.parse(stdout)
}

function summary(preview: Preview): string {
  const counts = preview.change_counts ?? {}
  const parts = [
    `新增 ${counts.new ?? 0}`,
    `修改 ${counts.modified ?? 0}`,
    `移动 ${counts.moved ?? 0}`,
    `恢复 ${counts.restored ?? 0}`,
    `删除 ${counts.deleted ?? 0}`,
  ]
  const pending = preview.pending_batch
    ? `\n待处理 batch：${preview.pending_batch}（共 ${preview.pending_count ?? 1} 个）`
    : ""
  return `${parts.join(" · ")}（共 ${preview.change_total ?? 0}）${pending}`
}

function dialogFor(api: Parameters<TuiPlugin>[0], provided?: TuiDialogStack): TuiDialogStack {
  return provided ?? api.ui.dialog
}

const mobileworkTui: TuiPlugin = async (api) => {
  const root = rootOf()
  let currentTier = readTier(root)
  const waitTimers = new Map<string, Array<ReturnType<typeof setTimeout>>>()

  const clearWaitTimers = (sessionID: string) => {
    for (const timer of waitTimers.get(sessionID) ?? []) clearTimeout(timer)
    waitTimers.delete(sessionID)
  }

  const watchLongResponse = (sessionID: string) => {
    clearWaitTimers(sessionID)
    waitTimers.set(sessionID, [
      setTimeout(() => api.ui.toast({
        title: "Mobilework",
        message: "仍在整理答案；可以继续等待，或按 Esc 安全中止",
        variant: "info",
      }), 45_000),
      setTimeout(() => api.ui.toast({
        title: "响应时间较长",
        message: "可以按 Esc 中止后重试；检索等级不会自动改变",
        variant: "warning",
      }), 90_000),
    ])
  }

  const stopStatusWatch = api.event.on("session.status", (event) => {
    if (event.properties.status.type === "busy") watchLongResponse(event.properties.sessionID)
    else clearWaitTimers(event.properties.sessionID)
  })
  const stopIdleWatch = api.event.on("session.idle", (event) => clearWaitTimers(event.properties.sessionID))
  const stopErrorWatch = api.event.on("session.error", (event) => {
    if (event.properties.sessionID) clearWaitTimers(event.properties.sessionID)
  })

  if (!api.command) {
    api.ui.toast({
      title: "Mobilework 插件不兼容",
      message: "当前 OpenCode 未提供命令注册接口，请升级 OpenCode。",
      variant: "error",
    })
    return
  }

  const unregister = api.command.register(() => [
    {
      title: `检索等级 · ${currentTier.toUpperCase()}`,
      value: "mobilework.retrieve",
      description: "选择 Naive / Low / Medium / High",
      category: "Mobilework",
      suggested: true,
      slash: { name: "retrieve", aliases: [] },
      onSelect: (provided) => {
        const dialog = dialogFor(api, provided)
        dialog.replace(() => api.ui.DialogSelect<Tier>({
          title: "选择检索等级",
          current: currentTier,
          skipFilter: true,
          options: TIERS,
          onSelect: async (option) => {
            try {
              await writeTier(root, option.value)
              currentTier = option.value
              dialog.clear()
              api.ui.toast({
                title: "Mobilework",
                message: `Retrieve: ${currentTier[0].toUpperCase()}${currentTier.slice(1)}`,
                variant: "success",
              })
            } catch (error) {
              dialog.clear()
              api.ui.toast({ title: "保存检索等级失败", message: String(error), variant: "error" })
            }
          },
        }))
      },
    },
    {
      title: "扫描并同步 Wiki",
      value: "mobilework.sync",
      description: "预览变化、确认后在当前会话构建",
      category: "Mobilework",
      suggested: true,
      slash: { name: "sync", aliases: [] },
      onSelect: async (provided) => {
        const dialog = dialogFor(api, provided)
        let preview: Preview
        try {
          preview = await runWiki(root, ["preview"])
        } catch (error) {
          dialog.replace(() => api.ui.DialogAlert({
            title: "同步扫描失败",
            message: String(error),
            onConfirm: () => dialog.clear(),
          }))
          return
        }

        if (!preview.pending_batch && preview.change_total === 0) {
          api.ui.toast({ title: "Mobilework", message: "没有需要同步的资料变化", variant: "success" })
          return
        }

        dialog.replace(() => api.ui.DialogConfirm({
          title: "确认同步 Wiki？",
          message: summary(preview),
          onCancel: () => dialog.clear(),
          onConfirm: async () => {
            dialog.clear()
            try {
              let sessionID: string
              if (api.route.current.name === "session") {
                sessionID = api.route.current.params.sessionID
              } else {
                const created = await api.client.session.create({
                  directory: root,
                  title: "Mobilework Wiki sync",
                  agent: "mobilework",
                })
                if (!created.data) throw new Error("OpenCode 未返回新会话")
                sessionID = created.data.id
                api.route.navigate("session", { sessionID })
              }
              await api.client.session.promptAsync({
                sessionID,
                directory: root,
                agent: "mobilework",
                parts: [{
                  type: "text",
                  text: `[MOBILEWORK_SYNC_CONFIRMED]\n用户已确认同步。扫描摘要：${summary(preview)}\n请按主 Agent 规则恰好委派一次 wiki-builder，并在当前会话报告结果。`,
                }],
              })
              api.ui.toast({ title: "Mobilework", message: "Wiki Builder 已开始", variant: "info" })
            } catch (error) {
              api.ui.toast({ title: "无法启动同步", message: String(error), variant: "error" })
            }
          },
        }))
      },
    },
  ])

  api.lifecycle.onDispose(() => {
    unregister()
    stopStatusWatch()
    stopIdleWatch()
    stopErrorWatch()
    for (const sessionID of waitTimers.keys()) clearWaitTimers(sessionID)
  })
  api.ui.toast({
    title: "Mobilework 已就绪",
    message: `Retrieve: ${currentTier[0].toUpperCase()}${currentTier.slice(1)} · /retrieve · /sync`,
    variant: "info",
  })
}

export default { id: "mobilework.cli", tui: mobileworkTui }
