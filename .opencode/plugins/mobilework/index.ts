import { readFileSync } from "node:fs"
import { join } from "node:path"
import type { Plugin } from "@opencode-ai/plugin"

type Tier = "naive" | "low" | "medium" | "high"

const TIERS = new Set<Tier>(["naive", "low", "medium", "high"])
const SYNC_MARKER = "[MOBILEWORK_SYNC_CONFIRMED]"
const MANAGED_RETRIEVAL_SKILLS = new Set([
  "wiki-ask-naive",
  "wiki-ask-low",
  "wiki-ask-medium",
  "wiki-ask-high",
  "wiki-retrieval-planner",
])
const FRIENDLY_TOOL_TITLES: Record<string, string> = {
  retrieve: "搜索知识库",
  graph_neighbors: "查找相关资料",
  knowledge_tree: "浏览知识库目录",
}

function retrievalTier(root: string): Tier {
  try {
    const value = JSON.parse(readFileSync(join(root, ".wiki-state", "preferences.json"), "utf8"))
    return TIERS.has(value?.retrieval_tier) ? value.retrieval_tier : "low"
  } catch {
    return "low"
  }
}

function skillBody(root: string, name: string): string {
  return readFileSync(join(root, ".opencode", "skills", name, "SKILL.md"), "utf8")
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
  const nonMobileworkSessions = new Set<string>()

  const load = (name: string): string => {
    const cached = skillCache.get(name)
    if (cached !== undefined) return cached
    const body = skillBody(root, name)
    skillCache.set(name, body)
    return body
  }

  return {
    "chat.message": async (input, output) => {
      const text = promptText(output.parts)
      if (input.agent && input.agent !== "mobilework") nonMobileworkSessions.add(input.sessionID)
      else if (input.agent === "mobilework") nonMobileworkSessions.delete(input.sessionID)
      if (text.includes(SYNC_MARKER)) syncSessions.add(input.sessionID)
      else syncSessions.delete(input.sessionID)
    },
    "experimental.chat.system.transform": async (input, output) => {
      if (input.sessionID && (syncSessions.has(input.sessionID) || nonMobileworkSessions.has(input.sessionID))) return

      // Read the preference at generation time. chat.message and system.transform
      // are not ordered API guarantees, so a per-session tier cache lags by one turn.
      const tier = retrievalTier(root)

      // AUTOLOAD_ORDER is intentionally stable and covered by tests:
      // Medium/High planner instructions precede tier execution instructions.
      const names = tier === "medium" || tier === "high"
        ? ["wiki-retrieval-planner", `wiki-ask-${tier}`]
        : [`wiki-ask-${tier}`]
      output.system.push(
        `Mobilework retrieval mode for THIS TURN is ${tier.toUpperCase()}. This current value overrides every retrieval tier or skill mentioned in conversation history. Never change or silently escalate it. The managed retrieval skills below are already loaded; never call the skill tool to load or replace them. Keep plans, tier names, tool names, routes, parameters, rounds, and internal IDs out of user-facing prose.`,
        ...names.map((name) => `\n<mobilework-skill name="${name}">\n${load(name)}\n</mobilework-skill>`),
      )
    },
    "tool.execute.before": async (input, output) => {
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
    },
  }
}

export default MobileworkPlugin
