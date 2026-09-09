import { appendFileSync, mkdirSync, readFileSync } from "node:fs"
import { join } from "node:path"
import type { Plugin } from "@opencode-ai/plugin"
import { readPreferences, resolveConfig, RetrievalGuard } from "./retrieval-config.mjs"

const SYNC_MARKER = "[MOBILEWORK_SYNC_CONFIRMED]"
const RETRY_ANSWER_MARKER = "[MOBILEWORK_RETRY_ANSWER_ONLY]"
const MANAGED_RETRIEVAL_SKILLS = new Set(["wiki-retrieval"])
const FRIENDLY_TOOL_TITLES: Record<string, string> = {
  retrieve: "搜索知识库",
  graph_neighbors: "查找相关资料",
  knowledge_tree: "浏览知识库目录",
  route_knowledge_bases: "选择相关知识库",
  list_knowledge_bases: "浏览可用知识库",
}

function skillBody(root: string, relativePath: string): string {
  return readFileSync(join(root, ".opencode", "skills", "wiki-retrieval", relativePath), "utf8")
}

function promptText(parts: any[]): string {
  return parts.filter((part) => part?.type === "text").map((part) => part.text ?? "").join("\n")
}

function retrievalToolKind(tool: string): string | undefined {
  return Object.keys(FRIENDLY_TOOL_TITLES).find((name) => tool === name || tool.endsWith(`_${name}`))
}

const MobileworkPlugin: Plugin = async ({ directory }) => {
  const root = process.env.MOBILEWORK_ROOT || directory || process.cwd()
  const skillCache = new Map<string, string>()
  const syncSessions = new Set<string>()
  const retryAnswerSessions = new Set<string>()
  const nonMobileworkSessions = new Set<string>()
  const guards = new Map<string, RetrievalGuard>()
  const requests = new Map<string, any>()

  const logGuardStop = (reason: string, kind: string, args: any) => {
    try {
      const directory = join(root, ".mobilework-state", "logs")
      mkdirSync(directory, { recursive: true })
      appendFileSync(
        join(directory, "retrieval.log"),
        `${new Date().toISOString()} WARN mobilework.plugin event=guard_stopped reason=${reason} tool=${kind} kb_count=${Array.isArray(args?.kb_ids) ? args.kb_ids.length : 0} scope=${String(args?.scope ?? "wiki")}\n`,
        "utf8",
      )
    } catch { /* Observability must never break retrieval. */ }
  }

  const load = (relativePath: string): string => {
    const cached = skillCache.get(relativePath)
    if (cached !== undefined) return cached
    const body = skillBody(root, relativePath)
    skillCache.set(relativePath, body)
    return body
  }

  return {
    "chat.params": async (input, output) => {
      if (input.agent !== "mobilework" || syncSessions.has(input.sessionID)) return
      const config = resolveConfig(readPreferences(root), requests.get(input.sessionID) ?? {})
      // Retrieval budgets do not bound the provider's generation allocation.
      // Keep short factual answers from requesting the model's maximum output.
      const limit = ["fast", "balanced"].includes(config.retrieval_profile) ? 2048 : 4096
      output.maxOutputTokens = Math.min(output.maxOutputTokens ?? limit, limit)
      if (input.model?.providerID === "openrouter" && input.model?.id === "qwen/qwen3.8-flash") {
        // This model defaults to reasoning and supports an explicit token budget.
        output.options.reasoning = ["fast", "balanced"].includes(config.retrieval_profile)
          ? { enabled: false }
          : { enabled: true, max_tokens: 1024 }
        output.options.provider = { ...output.options.provider, sort: "latency" }
      }
    },
    "chat.message": async (input, output) => {
      const text = promptText(output.parts)
      // Machine-readable per-turn overrides may be supplied by a client text part.
      const override = text.match(/<mobilework-retrieval>([\s\S]*?)<\/mobilework-retrieval>/)
      try { requests.set(input.sessionID, override ? JSON.parse(override[1]) : {}) } catch { requests.set(input.sessionID, {}) }
      guards.delete(input.sessionID)
      if (input.agent && input.agent !== "mobilework") nonMobileworkSessions.add(input.sessionID)
      else if (input.agent === "mobilework") nonMobileworkSessions.delete(input.sessionID)
      if (text.includes(SYNC_MARKER)) syncSessions.add(input.sessionID)
      else syncSessions.delete(input.sessionID)
      if (text.includes(RETRY_ANSWER_MARKER)) retryAnswerSessions.add(input.sessionID)
      else retryAnswerSessions.delete(input.sessionID)
    },
    "experimental.chat.system.transform": async (input, output) => {
      if (input.sessionID && (syncSessions.has(input.sessionID) || nonMobileworkSessions.has(input.sessionID))) return

      if (input.sessionID && retryAnswerSessions.has(input.sessionID)) {
        output.system.push(
          "This is a timeout recovery turn. Answer the preceding user question using only the most recent completed retrieval result already present in the conversation. Do not call any retrieval tool or repeat the lookup. If that result has no usable evidence, say so briefly.",
        )
        return
      }

      // Read the preference at generation time. chat.message and system.transform
      // are not ordered API guarantees, so a per-session tier cache lags by one turn.
      const config = resolveConfig(readPreferences(root), requests.get(input.sessionID ?? "") ?? {})
      const profileReference = `references/profiles/${config.retrieval_profile}.md`
      output.system.push(
        `Mobilework retrieval configuration for THIS TURN is ${JSON.stringify(config)}. Never change or silently escalate it. This value overrides historical settings. For an ordinary knowledge-base question call retrieve directly; it performs routing internally, so do not preflight with list_knowledge_bases or route_knowledge_bases. The runtime injects profile, channels, and overrides into retrieve; do not construct or pass those arguments yourself. Every factual answer with usable retrieved evidence must include inline [n] citations and a final 参考证据 mapping based on returned citation metadata. Managed skills are already loaded; never call skill to replace them. Keep plans, tool names, parameters, and internal IDs out of user-facing prose.`,
        `\n<mobilework-skill name="wiki-retrieval">\n${load("SKILL.md")}\n</mobilework-skill>`,
        `\n<mobilework-profile name="${config.retrieval_profile}">\n${load(profileReference)}\n</mobilework-profile>`,
      )
    },
    "tool.execute.before": async (input, output) => {
      const kind = retrievalToolKind(input.tool)
      if (kind && !nonMobileworkSessions.has(input.sessionID) && !syncSessions.has(input.sessionID)) {
        output.title = FRIENDLY_TOOL_TITLES[kind]
        if (!["route_knowledge_bases", "list_knowledge_bases"].includes(kind)) {
          const config = resolveConfig(readPreferences(root), requests.get(input.sessionID) ?? {})
          let guard = guards.get(input.sessionID)
          if (!guard) { guard = new RetrievalGuard(config); guards.set(input.sessionID, guard) }
          const kbBoundary = kind === "retrieve"
            ? [...(output.args?.kb_ids ?? [])].map(String).sort().join(",")
            : ""
          const scopeBoundary = kind === "retrieve" ? String(output.args?.scope ?? "wiki") : ""
          const stop = guard.before(`${kind}:${output.args?.query ?? JSON.stringify(output.args)}:kb=${kbBoundary}:scope=${scopeBoundary}`)
          if (stop) {
            logGuardStop(stop, kind, output.args)
            throw new Error(`Retrieval stopped: ${stop}; answer using existing evidence and disclose gaps`)
          }
          if (kind === "retrieve") {
            output.args.profile = config.retrieval_profile
            output.args.channels = ["vector", "keyword", "graph"].filter(key => config.retrieval[key])
            output.args.overrides = { retrieval: config.retrieval, budget: config.budget }
          }
        }
      }
      if (retryAnswerSessions.has(input.sessionID) && retrievalToolKind(input.tool)) {
        throw new Error("Timeout recovery must reuse the completed retrieval result and cannot retrieve again")
      }
      if (input.tool !== "skill") return
      const name = String(output.args?.name ?? output.args?.skill ?? "")
      if (MANAGED_RETRIEVAL_SKILLS.has(name)) {
        throw new Error(`Mobilework manages retrieval skill '${name}' automatically for the current turn`)
      }
    },
    "tool.execute.after": async (input, output) => {
      const kind = retrievalToolKind(input.tool)
      if (!kind) return
      output.title = FRIENDLY_TOOL_TITLES[kind]
      if (!["route_knowledge_bases", "list_knowledge_bases"].includes(kind)) {
        try {
          const data = JSON.parse(output.output)
          const results = data.results ?? data.hits ?? data.neighbors ?? []
          guards.get(input.sessionID)?.after(results.map((hit: any) => `${hit.kb_id ?? ""}:${hit.chunk_id ?? hit.page_id ?? hit.id ?? JSON.stringify(hit)}`), data.evidence_sufficient === true, data.duplicate === true)
        } catch { /* Non-JSON tool results do not prove absence of new evidence. */ }
      }
    },
  }
}

export default MobileworkPlugin
