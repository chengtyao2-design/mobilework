import { readFileSync } from 'node:fs'
import { join } from 'node:path'

export const PROFILE_NAMES = ['fast', 'balanced', 'reasoning', 'research']
export const LEGACY_PROFILES = { naive: 'fast', low: 'balanced', medium: 'reasoning', high: 'research' }
export const SWITCHES = ['vector', 'keyword', 'graph', 'decompose', 'raw_evidence_fallback', 'sufficiency_check', 'auto_supplement', 'rerank', 'cross_kb_entity_expand', 'answer_writeback']
export const SWITCH_LABELS = {
  vector: '向量检索', keyword: '关键词检索', graph: '图关系扩展',
  decompose: '问题分解（Agent 执行）', raw_evidence_fallback: '原始资料回查（Agent 执行）',
  sufficiency_check: '证据充分性检查（Agent 执行）', auto_supplement: '自动补检（Agent 执行）',
  rerank: '模型重排（暂未接入）', cross_kb_entity_expand: '跨库实体扩展（暂未接入）',
  answer_writeback: '回答写回（暂未接入）',
}
export const UNAVAILABLE_SWITCHES = ['rerank', 'cross_kb_entity_expand', 'answer_writeback']
export const FRESHNESS_LABELS = { off: '关闭', context: '仅提供时间上下文', rerank: '时间加权重排', both: '时间上下文＋重排' }
const base = Object.fromEntries(SWITCHES.map(key => [key, ['vector', 'keyword', 'raw_evidence_fallback'].includes(key)]))
export const PROFILES = {
  fast: { retrieval: { ...base, keyword: false, raw_evidence_fallback: false }, budget: { deadline_ms: 5000, max_tool_calls: 1, max_subqueries: 1, max_graph_depth: 0, max_evidence_chars: 6000 } },
  balanced: { retrieval: { ...base }, budget: { deadline_ms: 12000, max_tool_calls: 1, max_subqueries: 1, max_graph_depth: 0, max_evidence_chars: 12000 } },
  reasoning: { retrieval: { ...base, graph: true, decompose: true, auto_supplement: true }, budget: { deadline_ms: 30000, max_tool_calls: 4, max_subqueries: 3, max_graph_depth: 1, max_evidence_chars: 18000 } },
  research: { retrieval: { ...base, graph: true, decompose: true, sufficiency_check: true, auto_supplement: true }, budget: { deadline_ms: 90000, max_tool_calls: 8, max_subqueries: 3, max_graph_depth: 2, max_evidence_chars: 24000 } },
}
export function readPreferences(root) {
  try { return JSON.parse(readFileSync(join(root, '.mobilework-state', 'preferences.json'), 'utf8')) } catch { return {} }
}
export function resolveConfig(saved = {}, request = {}) {
  const candidate = request.retrieval_profile ?? saved.retrieval_profile ?? LEGACY_PROFILES[saved.retrieval_tier]
  const profile = PROFILE_NAMES.includes(candidate) ? candidate : 'balanced'
  const result = { retrieval_profile: profile, retrieval: { ...PROFILES[profile].retrieval, claim_freshness_mode: 'off' }, budget: { ...PROFILES[profile].budget } }
  for (const layer of [saved, request]) {
    for (const key of SWITCHES) if (typeof layer.retrieval?.[key] === 'boolean') result.retrieval[key] = layer.retrieval[key]
    if (['off', 'context', 'rerank', 'both'].includes(layer.retrieval?.claim_freshness_mode)) result.retrieval.claim_freshness_mode = layer.retrieval.claim_freshness_mode
    for (const key of Object.keys(result.budget)) {
      const n = layer.budget?.[key]
      if (Number.isInteger(n) && n >= (key === 'max_graph_depth' ? 0 : 1)) result.budget[key] = n
    }
  }
  for (const key of UNAVAILABLE_SWITCHES) result.retrieval[key] = false
  return result
}
// A round is one retrieval/tool request; catalog routing is metadata, not a round.
export class RetrievalGuard {
  constructor(config, now = Date.now()) { this.config = config; this.started = now; this.calls = 0; this.queries = new Set(); this.evidence = new Set(); this.reason = null }
  before(query, now = Date.now()) {
    if (this.reason) return this.reason
    if (now - this.started >= this.config.budget.deadline_ms) return (this.reason = 'deadline')
    if (this.calls >= this.config.budget.max_tool_calls) return (this.reason = 'call_limit')
    const normalized = String(query).normalize('NFKC').toLowerCase().replace(/[\p{P}\p{Z}\s]/gu, '')
    if (this.queries.has(normalized)) return (this.reason = 'duplicate_query')
    this.queries.add(normalized); this.calls++; return null
  }
  after(ids = [], sufficient = false, duplicate = false) {
    if (duplicate) return (this.reason = 'duplicate_query')
    if (sufficient) return (this.reason = 'evidence_sufficient')
    const fresh = ids.filter(id => !this.evidence.has(id))
    for (const id of fresh) this.evidence.add(id)
    if (!fresh.length) this.reason = 'no_new_evidence'
    return this.reason
  }
}
