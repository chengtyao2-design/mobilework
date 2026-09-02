import { readFileSync } from "node:fs"
import { join } from "node:path"
import type { Plugin } from "@opencode-ai/plugin"

type Tier = "naive" | "low" | "medium" | "high"

const TIERS = new Set<Tier>(["naive", "low", "medium", "high"])
const SYNC_MARKER = "[MOBILEWORK_SYNC_CONFIRMED]"

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

const MobileworkPlugin: Plugin = async ({ directory }) => {
  const root = process.env.MOBILEWORK_ROOT || directory || process.cwd()
  const skillCache = new Map<string, string>()
  const sessionTier = new Map<string, Tier | null>()

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
      if (input.agent !== "mobilework" || text.includes(SYNC_MARKER)) {
        sessionTier.set(input.sessionID, null)
        return
      }
      sessionTier.set(input.sessionID, retrievalTier(root))
    },
    "experimental.chat.system.transform": async (input, output) => {
      const tier = input.sessionID ? sessionTier.get(input.sessionID) : undefined
      if (!tier) return

      // AUTOLOAD_ORDER is intentionally stable and covered by tests:
      // Medium/High planner instructions precede tier execution instructions.
      const names = tier === "medium" || tier === "high"
        ? ["wiki-retrieval-planner", `wiki-ask-${tier}`]
        : [`wiki-ask-${tier}`]
      output.system.push(
        `Mobilework retrieval mode is ${tier.toUpperCase()}. Never change or silently escalate it.`,
        ...names.map((name) => `\n<mobilework-skill name="${name}">\n${load(name)}\n</mobilework-skill>`),
      )
    },
  }
}

export default MobileworkPlugin
