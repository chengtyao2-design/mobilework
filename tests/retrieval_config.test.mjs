import { test } from 'node:test'
import assert from 'node:assert/strict'
import { resolveConfig, RetrievalGuard, PROFILE_NAMES } from '../.opencode/plugins/mobilework/retrieval-config.mjs'
import MobileworkPlugin from '../.opencode/plugins/mobilework/index.ts'
import { fileURLToPath } from 'node:url'

test('four profiles and conservative balanced defaults', () => {
  assert.deepEqual(PROFILE_NAMES, ['fast', 'balanced', 'reasoning', 'research'])
  const c = resolveConfig()
  assert.equal(c.retrieval_profile, 'balanced')
  assert.equal(c.retrieval.vector, true); assert.equal(c.retrieval.keyword, true)
  for (const key of ['graph', 'decompose', 'sufficiency_check', 'auto_supplement', 'rerank', 'cross_kb_entity_expand', 'answer_writeback']) assert.equal(c.retrieval[key], false)
  assert.equal(c.retrieval.claim_freshness_mode, 'off')
  assert.equal(c.budget.max_tool_calls, 1)
})
test('request wins over saved overrides over profile; validate values', () => {
  const c = resolveConfig({ retrieval_profile: 'research', retrieval: { graph: false, keyword: false }, budget: { deadline_ms: 2000 } }, { retrieval: { graph: true }, budget: { deadline_ms: 4000, max_tool_calls: -1 } })
  assert.equal(c.retrieval.graph, true); assert.equal(c.retrieval.keyword, false)
  assert.equal(c.budget.deadline_ms, 4000); assert.equal(c.budget.max_tool_calls, 8)
  assert.equal(resolveConfig({retrieval_tier: 'high'}).retrieval_profile, 'research')
  assert.equal(resolveConfig({retrieval_profile:'nonsense'}).retrieval_profile, 'balanced')
})
test('guard deadline, call limit, normalization, evidence and backend semantic duplication', () => {
  const config = resolveConfig({retrieval_profile:'research'})
  let g = new RetrievalGuard(config, 0)
  assert.equal(g.before('VAT?', 1), null)
  assert.equal(g.before(' vat ', 2), 'duplicate_query')
  g = new RetrievalGuard(config, 0)
  assert.equal(g.before('q', 90000), 'deadline')
  g = new RetrievalGuard(resolveConfig(), 0)
  assert.equal(g.before('q', 1), null); assert.equal(g.before('q2', 2), 'call_limit')
  g = new RetrievalGuard(config, 0)
  assert.equal(g.after(['kb:a']), null); assert.equal(g.after(['kb:a']), 'no_new_evidence')
  g = new RetrievalGuard(config, 0); assert.equal(g.after(['x'], true), 'evidence_sufficient')
  g = new RetrievalGuard(config, 0); assert.equal(g.after([], false, true), 'duplicate_query')
})
test('plugin injects current config and enforces unified calls without widening KB ids', async () => {
  const hooks = await MobileworkPlugin({ directory: fileURLToPath(new URL('..', import.meta.url)) })
  await hooks['chat.message']({sessionID:'test', agent:'mobilework'}, {parts:[{type:'text',text:'<mobilework-retrieval>{"retrieval_profile":"research","retrieval":{"graph":false}}</mobilework-retrieval>'}]})
  const system = {system:[]}
  await hooks['experimental.chat.system.transform']({sessionID:'test'}, system)
  assert.ok(system.system.join('\n').includes('"retrieval_profile":"research"'))
  const output = {args:{query:'query',kb_ids:['kb_a']}}
  await hooks['tool.execute.before']({sessionID:'test',tool:'wiki_retrieve'}, output)
  assert.deepEqual(output.args.kb_ids, ['kb_a']); assert.equal(output.args.profile, 'research'); assert.equal(output.args.overrides.retrieval.graph,false)
  await assert.rejects(() => hooks['tool.execute.before']({sessionID:'test',tool:'wiki_retrieve'}, {args:{query:'Query?'}}), /duplicate_query/)
})
