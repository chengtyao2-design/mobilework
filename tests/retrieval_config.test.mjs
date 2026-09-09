import { test } from 'node:test'
import assert from 'node:assert/strict'
import { resolveConfig, responseWatchdogMs, RetrievalGuard, PROFILE_NAMES } from '../.opencode/plugins/mobilework/retrieval-config.mjs'
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
  assert.equal(resolveConfig({}, {retrieval_tier: 'naive'}).retrieval_profile, 'fast')
  assert.equal(resolveConfig({retrieval_profile:'nonsense'}).retrieval_profile, 'balanced')
})
test('advanced profiles allow retrieval budget plus answer grace before watchdog abort', () => {
  assert.equal(responseWatchdogMs(resolveConfig({retrieval_profile:'fast'})), 45_000)
  assert.equal(responseWatchdogMs(resolveConfig({retrieval_profile:'balanced'})), 45_000)
  assert.equal(responseWatchdogMs(resolveConfig({retrieval_profile:'reasoning'})), 75_000)
  assert.equal(responseWatchdogMs(resolveConfig({retrieval_profile:'research'})), 135_000)
  assert.equal(responseWatchdogMs(resolveConfig({retrieval_profile:'research', budget:{deadline_ms:120_000}})), 165_000)
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
  assert.ok(system.system.join('\n').includes('<mobilework-skill name="wiki-retrieval">'))
  assert.ok(system.system.join('\n').includes('<mobilework-profile name="research">'))
  assert.ok(system.system.join('\n').includes('max_tool_calls: 8'))
  assert.ok(system.system.join('\n').includes('do not preflight with list_knowledge_bases or route_knowledge_bases'))
  assert.ok(system.system.join('\n').includes('do not construct or pass those arguments yourself'))
  assert.ok(!system.system.join('\n').includes('<mobilework-profile name="fast">'))
  const output = {args:{query:'query',kb_ids:['kb_a']}}
  await hooks['tool.execute.before']({sessionID:'test',tool:'wiki_retrieve'}, output)
  assert.deepEqual(output.args.kb_ids, ['kb_a']); assert.equal(output.args.profile, 'research'); assert.equal(output.args.overrides.retrieval.graph,false)
  assert.deepEqual(output.args.channels, ['vector', 'keyword'])
  const otherKb = {args:{query:'Query?',kb_ids:['kb_b']}}
  await hooks['tool.execute.before']({sessionID:'test',tool:'wiki_retrieve'}, otherKb)
  assert.deepEqual(otherKb.args.kb_ids, ['kb_b'])
  await assert.rejects(() => hooks['tool.execute.before']({sessionID:'test',tool:'wiki_retrieve'}, {args:{query:'Query?',kb_ids:['kb_b']}}), /duplicate_query/)
})

test('all profiles bound model generation independently of retrieval budget', async () => {
  const hooks = await MobileworkPlugin({ directory: fileURLToPath(new URL('..', import.meta.url)) })
  for (const profile of PROFILE_NAMES) {
    await hooks['chat.message']({sessionID:profile, agent:'mobilework'}, {parts:[{type:'text',text:`<mobilework-retrieval>{"retrieval_profile":"${profile}"}</mobilework-retrieval>`}]})
    const params = {maxOutputTokens: 65536, options: {}}
    await hooks['chat.params']({sessionID:profile, agent:'mobilework',model:{providerID:'openrouter',id:'qwen/qwen3.8-flash'}}, params)
    assert.equal(params.maxOutputTokens, ['fast','balanced'].includes(profile) ? 2048 : 4096)
    assert.equal(params.options.reasoning.enabled, !['fast','balanced'].includes(profile))
    const args = {args:{query:'PBC',channels:['vector']}}
    await hooks['tool.execute.before']({sessionID:profile, tool:'wiki-retrieval_retrieve'}, args)
    assert.deepEqual(args.args.channels, profile === 'fast' ? ['vector'] : profile === 'balanced' ? ['vector','keyword'] : ['vector','keyword','graph'])
  }
})
