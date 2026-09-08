import { test } from 'node:test'
import assert from 'node:assert/strict'
import plugin from '../.opencode/plugins/mobilework/tui.ts'
import { resolveConfig, SWITCH_LABELS } from '../.opencode/plugins/mobilework/retrieval-config.mjs'

async function harness(t) {
  t.mock.timers.enable({apis:['setTimeout']})
  const handlers = {}, toasts = [], dialogs = [], prompts = [], aborted = [], selections = []
  let commands
  const api = {
    event: {on(name, handler) {handlers[name] = handler; return () => {}}},
    ui: {toast: x => toasts.push(x), dialog: {clear() {}, replace(fn) {fn()}}, DialogConfirm: x => dialogs.push(x), DialogSelect: x => selections.push(x)},
    client: {session: {async abort(x) {aborted.push(x); handlers['session.status']({properties:{sessionID:x.sessionID,status:{type:'idle'}}})}, async promptAsync(x) {prompts.push(x)}}},
    command: {register(factory) {commands = factory(); return () => {}}}, lifecycle: {onDispose() {}},
  }
  await plugin.tui(api)
  const event = (name, properties) => handlers[name]({properties})
  return {event, toasts, dialogs, prompts, aborted, commands, selections}
}

test('silent initial model stops after 45 seconds and recovery performs retrieval', async t => {
  const h = await harness(t)
  h.event('session.status', {sessionID:'s',status:{type:'busy'}})
  t.mock.timers.tick(15000)
  assert.match(h.toasts.at(-1).message, /尚未取得/)
  h.event('session.status', {sessionID:'s',status:{type:'retry'}})
  t.mock.timers.tick(30000)
  await Promise.resolve()
  assert.equal(h.aborted.length,1)
  await h.dialogs[0].onConfirm()
  assert.doesNotMatch(h.prompts[0].parts[0].text, /MOBILEWORK_RETRY_ANSWER_ONLY/)
})

test('progress renews idle deadline and completed evidence survives abort event', async t => {
  const h = await harness(t)
  h.event('session.status', {sessionID:'s',status:{type:'busy'}})
  t.mock.timers.tick(30000)
  h.event('message.part.updated', {part:{sessionID:'s',type:'tool',tool:'wiki-retrieval_retrieve',state:{status:'completed'}}})
  t.mock.timers.tick(30000)
  assert.equal(h.aborted.length,0)
  t.mock.timers.tick(15000)
  await Promise.resolve()
  await h.dialogs[0].onConfirm()
  assert.match(h.prompts[0].parts[0].text, /MOBILEWORK_RETRY_ANSWER_ONLY/)
})

test('unsupported options cannot be activated via saved or request settings', () => {
  const c = resolveConfig({retrieval:{rerank:true}}, {retrieval:{cross_kb_entity_expand:true,answer_writeback:true}})
  assert.equal(c.retrieval.rerank,false)
  assert.equal(c.retrieval.cross_kb_entity_expand,false)
  assert.equal(c.retrieval.answer_writeback,false)
  assert.equal(SWITCH_LABELS.vector,'向量检索')
})

test('advanced menu displays Chinese names and refuses unsupported toggles', async t => {
  const h = await harness(t)
  h.commands.find(c => c.value === 'mobilework.retrieve.options').onSelect()
  const menu = h.selections[0]
  assert.match(menu.options.find(o => o.value === 'vector').title, /^向量检索：/)
  const rerank = menu.options.find(o => o.value === 'rerank')
  assert.match(rerank.title, /暂未接入.*不可用/)
  await menu.onSelect(rerank)
  assert.equal(h.toasts.at(-1).title, '该能力暂未接入')
})
