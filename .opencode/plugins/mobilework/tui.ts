import { mkdir, rename, writeFile } from "node:fs/promises"
import { dirname, join } from "node:path"
import type { TuiDialogStack, TuiPlugin } from "@opencode-ai/plugin/tui"
import { readPreferences, resolveConfig, SWITCHES, SWITCH_LABELS, UNAVAILABLE_SWITCHES, FRESHNESS_LABELS } from "./retrieval-config.mjs"

type Tier = "fast" | "balanced" | "reasoning" | "research"
type Preview = {
  pending_batch?: string | null
  pending_count?: number
  change_total: number
  change_counts: Record<string, number>
}

const TIERS: Array<{ title: string; value: Tier; description: string }> = [
  { title: "快速", value: "fast", description: "5 秒检索预算 / 一次单通道召回" },
  { title: "均衡", value: "balanced", description: "12 秒检索预算 / 一次混合召回（默认）" },
  { title: "推理", value: "reasoning", description: "30 秒检索预算 / 最多四次检索" },
  { title: "研究", value: "research", description: "90 秒检索预算 / 最多八次检索" },
]

function rootOf(): string {
  return process.env.MOBILEWORK_ROOT || process.cwd()
}

function preferencePath(root: string): string {
  return join(root, ".mobilework-state", "preferences.json")
}

function readTier(root: string): Tier {
  return resolveConfig(readPreferences(root)).retrieval_profile as Tier
}

async function writeTier(root: string, tier: Tier): Promise<void> {
  const target = preferencePath(root)
  const temporary = `${target}.tmp`
  await mkdir(dirname(target), { recursive: true })
  await writeFile(temporary, `${JSON.stringify({
    ...readPreferences(root),
    version: 3,
    retrieval_profile: tier,
    retrieval: {},
    budget: {},
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
  const kbRoot = process.env.MOBILEWORK_KB_ROOT || root
  const child = bun.spawn([python, "-m", "wiki_maintainer", "--root", root, "--kb-root", kbRoot, ...args], {
    cwd: root,
    env: { ...process.env, MOBILEWORK_KB_ROOT: kbRoot },
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
  const retrieved = new Set<string>()

  const clearWaitTimers = (sessionID: string) => {
    for (const timer of waitTimers.get(sessionID) ?? []) clearTimeout(timer)
    waitTimers.delete(sessionID)
  }

  const watchLongResponse = (sessionID: string) => {
    // One watchdog per busy period: repeated busy events must not postpone it.
    if (waitTimers.has(sessionID)) return
    waitTimers.set(sessionID, [
      setTimeout(() => api.ui.toast({
        title: "Mobilework",
        message: retrieved.has(sessionID) ? "资料已取得，正在等待模型回答；Esc 可停止" : "正在等待模型响应，尚未取得检索资料；Esc 可停止",
        variant: "info",
      }), 15_000),
      setTimeout(async () => {
        const hasEvidence = retrieved.has(sessionID)
        clearWaitTimers(sessionID)
        try {
          await api.client.session.abort({ sessionID, directory: root })
        } catch (error) {
          api.ui.toast({ title: "无法停止超时响应", message: String(error), variant: "error" })
          return
        }

        const dialog = api.ui.dialog
        dialog.replace(() => api.ui.DialogConfirm({
          title: "响应超时，已停止",
          message: hasEvidence ? "模型连续 45 秒无进展。是否使用已取得的资料重新生成回答？" : "模型连续 45 秒无进展，尚未取得检索资料。是否重新尝试上一问题？也可取消后切换模型。",
          onCancel: () => dialog.clear(),
          onConfirm: async () => {
            dialog.clear()
            try {
              await api.client.session.promptAsync({
                sessionID,
                directory: root,
                agent: "mobilework",
                parts: [{
                  type: "text",
                  text: hasEvidence ? "[MOBILEWORK_RETRY_ANSWER_ONLY]\n请只使用本会话最近一次已完成检索返回的资料直接回答上一问题；不要再次检索。" : "请重新处理本会话上一条用户问题，按当前检索设置查找资料并回答。",
                }],
              })
              api.ui.toast({ title: "Mobilework", message: hasEvidence ? "正在使用已有资料重新生成回答" : "正在重试上一问题", variant: "info" })
            } catch (error) {
              api.ui.toast({ title: "无法重新生成回答", message: String(error), variant: "error" })
            }
          },
        }))
      }, 45_000),
    ])
  }

  const stopStatusWatch = api.event.on("session.status", (event) => {
    if (["busy", "retry"].includes(event.properties.status.type)) watchLongResponse(event.properties.sessionID)
    else { clearWaitTimers(event.properties.sessionID); retrieved.delete(event.properties.sessionID) }
  })
  const stopIdleWatch = api.event.on("session.idle", (event) => {
    clearWaitTimers(event.properties.sessionID)
  })
  const stopErrorWatch = api.event.on("session.error", (event) => {
    if (event.properties.sessionID) clearWaitTimers(event.properties.sessionID)
  })
  const stopPartWatch = api.event.on("message.part.updated", (event) => {
    const { part } = event.properties
    const sessionID = part.sessionID ?? event.properties.sessionID
    if (!waitTimers.has(sessionID)) return
    if (part.type === "tool" && part.state.status === "completed" && /(?:^|_)retrieve$/.test(part.tool)) retrieved.add(sessionID)
    clearWaitTimers(sessionID)
    watchLongResponse(sessionID)
  })
  const stopDeltaWatch = api.event.on("message.part.delta", (event) => {
    const { sessionID, delta } = event.properties
    if (!delta || !waitTimers.has(sessionID)) return
    clearWaitTimers(sessionID)
    watchLongResponse(sessionID)
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
      title: `检索等级 · ${TIERS.find(tier => tier.value === currentTier)?.title}`,
      value: "mobilework.retrieve",
      description: "选择快速 / 均衡 / 推理 / 研究（重置高级覆盖）",
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
                message: `检索模式：${TIERS.find(tier => tier.value === currentTier)?.title}`,
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
      title: "检索高级开关",
      value: "mobilework.retrieve.options",
      description: "设置检索能力和断言新鲜度；标注 Agent 执行与暂未接入的选项",
      category: "Mobilework",
      slash: { name: "retrieve-options", aliases: [] },
      onSelect: (provided) => {
        const dialog = dialogFor(api, provided)
        const saved = readPreferences(root)
        const config = resolveConfig(saved)
        const options = [...SWITCHES.map(key => ({ title: `${SWITCH_LABELS[key]}：${UNAVAILABLE_SWITCHES.includes(key) ? "不可用" : config.retrieval[key] ? "开" : "关"}`, value: key })), { title: `断言新鲜度：${FRESHNESS_LABELS[config.retrieval.claim_freshness_mode]}`, value: "claim_freshness_mode" }]
        dialog.replace(() => api.ui.DialogSelect<string>({
          title: "切换独立能力（再次打开可继续设置）", options,
          onSelect: async (option) => {
            try {
              const key = option.value
              if (UNAVAILABLE_SWITCHES.includes(key)) {
                api.ui.toast({ title: "该能力暂未接入", message: "当前版本不支持启用此功能", variant: "info" })
                return
              }
              const modes = ["off", "context", "rerank", "both"]
              const value = key === "claim_freshness_mode" ? modes[(modes.indexOf(config.retrieval[key]) + 1) % modes.length] : !config.retrieval[key]
              if (["vector", "keyword", "graph"].includes(key) && value === false && !["vector", "keyword", "graph"].some(k => k !== key && config.retrieval[k])) {
                api.ui.toast({ title: "至少保留一种检索通道", message: "请先打开另一种检索通道", variant: "info" })
                return
              }
              const target = preferencePath(root)
              await mkdir(dirname(target), { recursive: true })
              await writeFile(`${target}.tmp`, JSON.stringify({ ...saved, version: 3, retrieval_profile: config.retrieval_profile, retrieval: { ...saved.retrieval, [key]: value } }, null, 2), "utf8")
              await rename(`${target}.tmp`, target)
              dialog.clear()
              api.ui.toast({ title: "已保存", message: key === "claim_freshness_mode" ? `断言新鲜度：${FRESHNESS_LABELS[String(value)]}` : `${SWITCH_LABELS[key]}：${value ? "开" : "关"}`, variant: "success" })
            } catch (error) { api.ui.toast({ title: "保存失败", message: String(error), variant: "error" }) }
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
                  text: `[MOBILEWORK_SYNC_CONFIRMED]\nMOBILEWORK_KB_ROOT=${process.env.MOBILEWORK_KB_ROOT || root}\n用户已确认同步。扫描摘要：${summary(preview)}\n请按主 Agent 规则恰好委派一次 wiki-builder，并在当前会话报告结果。`,
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
    stopPartWatch()
    stopDeltaWatch()
    for (const sessionID of waitTimers.keys()) clearWaitTimers(sessionID)
  })
  api.ui.toast({
    title: "Mobilework 已就绪",
    message: `检索模式：${TIERS.find(tier => tier.value === currentTier)?.title} · /retrieve · /sync`,
    variant: "info",
  })
}

export default { id: "mobilework.cli", tui: mobileworkTui }
