import assert from 'node:assert/strict'
import { test } from 'node:test'
import { createChatObservation, type ChatTransport } from '../src/chat/observation.ts'
import type { ChatEvent, ChatMessage, ChatRun } from '../src/api.ts'

const message: ChatMessage = { id: 2, role: 'assistant', content: '完整回答', citations: [{ id: 'S001', start: 12 }], created_at: '2026-10-13T01:00:01Z', status: 'ok' }
const user: ChatMessage = { id: 1, role: 'user', content: '问题', citations: [], created_at: '2026-10-13T01:00:00Z' }
const options = { allow_outline_edit: false, note_target: { name: '笔记.md', revision: 'r1' } }
const done: ChatRun = {
  id: 'run1', conversation_id: 'c1', question: '问题', options, status: 'done', created_at: user.created_at,
  pending: { phase: 'answer', expanded: { t: 2, images: 1 }, text: message.content, corrected: true, activity: [], usage: { total: 12 }, note_updates: 1 },
  context: null, context_current: false, compacting: false, preview: null, message, error: null,
}
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(r => { resolve = r })
  return { promise, resolve }
}
function fixture() {
  let posts = 0, stops = 0, notes = 0
  let emit: (event: ChatEvent) => void = () => { throw new Error('No stream') }
  const stream = deferred<void>()
  const transport: ChatTransport = {
    listChatConversations: async () => ({ current_id: 'c1', conversations: [] }),
    getChat: async () => [], getChatRun: async () => null, getChatContext: async () => null,
    askStream: async (_video, _question, callback) => { posts++; emit = callback; await stream.promise },
    stopChat: async () => { stops++; return { ok: true } },
  }
  const observer = createChatObservation('v1', transport, () => notes++)
  return { observer, transport, stream, emit: (event: ChatEvent) => emit(event), counts: () => ({ posts, stops, notes }) }
}

test('continuous events and restored snapshot produce the same answer metadata', async () => {
  const live = fixture()
  await live.observer.reload()
  const sending = live.observer.send('问题', options)
  live.emit({ type: 'phase', phase: 'answer', expanded: { t: 2, images: 1 } })
  live.emit({ type: 'delta', text: '部分' })
  live.emit({ type: 'replace', text: message.content })
  live.emit({ type: 'usage', usage: { total: 12 } })
  live.emit({ type: 'note_changed' })
  live.emit({ type: 'done', message })
  live.emit({ type: 'delta', text: '过期事件' })
  live.stream.resolve()
  await sending

  const restored = fixture()
  restored.transport.getChat = async () => [user, message]
  restored.transport.getChatRun = async () => ({ ...done, options: { note_target: options.note_target } })
  await restored.observer.reload()
  assert.deepEqual(restored.observer.getSnapshot().messages.at(-1), live.observer.getSnapshot().messages.at(-1))
  assert.equal(restored.observer.getSnapshot().messages.length, 2)
  assert.equal(restored.counts().posts, 0)
  assert.equal(live.observer.getSnapshot().pending, null)
})

test('disconnect recovers the same run without posting or repeating note notifications', async () => {
  const f = fixture()
  await f.observer.reload()
  let runId = ''
  f.transport.askStream = async (_v, _q, emit, _signal, opts) => {
    runId = opts!.run_id!
    emit({ type: 'note_changed' })
    emit({ type: 'delta', text: '部分' })
    throw new Error('network disconnected')
  }
  let reads = 0
  f.transport.getChatRun = async () => {
    reads++
    return { ...done, id: runId, status: reads === 1 ? 'running' : 'done', message: reads === 1 ? null : message }
  }
  await f.observer.send('问题', options)
  assert.equal(reads, 2)
  assert.equal(f.counts().notes, 2, 'one note change and one terminal refresh; replay must not notify again')
  assert.equal(f.observer.getSnapshot().messages.filter(m => m.role === 'assistant').length, 1)
  assert.equal(f.observer.getSnapshot().error, null)
})

test('disconnecting an observer does not stop the backend or accept late events', async () => {
  const f = fixture()
  await f.observer.reload()
  const sending = f.observer.send('问题', options)
  f.emit({ type: 'delta', text: '部分' })
  f.observer.disconnect()
  f.transport.listChatConversations = async () => ({ current_id: 'c2', conversations: [] })
  await f.observer.reload()
  f.emit({ type: 'done', message })
  f.stream.resolve()
  await sending
  assert.equal(f.counts().stops, 0)
  assert.deepEqual(f.observer.getSnapshot().messages, [])
  assert.equal(f.observer.getSnapshot().sessions?.current_id, 'c2')
})

test('explicit stop targets the run and failure retains partial text and retry attachments', async () => {
  const f = fixture()
  await f.observer.reload()
  let postedId: string | undefined, stoppedId: string | undefined
  f.transport.askStream = async (_v, _q, emit, _signal, opts) => {
    postedId = opts?.run_id
    emit({ type: 'delta', text: '已生成的部分' })
    await f.stream.promise
    emit({ type: 'error', error: '回答已停止' })
  }
  f.transport.stopChat = async (_v, id) => { stoppedId = id; return { ok: true } }
  const sending = f.observer.send('问题', options)
  await f.observer.stop()
  assert.ok(postedId)
  assert.equal(stoppedId, postedId)
  f.stream.resolve()
  await sending
  const failed = f.observer.getSnapshot().messages.at(-1)!
  assert.equal(failed.content, '已生成的部分')
  assert.equal(failed.status, 'failed')
  assert.equal(failed.error, '回答已停止')
  assert.deepEqual(failed.options, options)
})

test('late history response cannot replace a newer conversation', async () => {
  const f = fixture()
  const history = deferred<ChatMessage[]>()
  f.transport.getChat = () => history.promise
  const oldLoad = f.observer.reload()
  await Promise.resolve()
  f.transport.getChat = async () => []
  f.transport.listChatConversations = async () => ({ current_id: 'c2', conversations: [] })
  await f.observer.reload()
  history.resolve([user, message])
  await oldLoad
  assert.deepEqual(f.observer.getSnapshot().messages, [])
  assert.equal(f.observer.getSnapshot().sessions?.current_id, 'c2')
})

test('recovery never attaches to a different run', async () => {
  const f = fixture()
  await f.observer.reload()
  f.transport.askStream = async () => { throw new Error('lost stream') }
  f.transport.getChatRun = async () => ({ ...done, id: 'unrelated' })
  await f.observer.send('问题', options)
  const answer = f.observer.getSnapshot().messages.at(-1)!
  assert.equal(answer.status, 'failed')
  assert.notEqual(answer.id, message.id)
})

test('rejected submission preserves the backend error without an automatic second POST', async () => {
  const f = fixture()
  await f.observer.reload()
  let attempts = 0
  f.transport.askStream = async () => { attempts++; throw new Error('请先完成证据处理') }
  await f.observer.send('问题', options)
  assert.equal(attempts, 1)
  assert.equal(f.observer.getSnapshot().messages.at(-1)?.error, '请先完成证据处理')
})

test('a disconnected module cannot send a delayed prepared attachment', async () => {
  const f = fixture()
  await f.observer.reload()
  f.observer.disconnect()
  f.stream.resolve()
  await f.observer.send('问题', options)
  assert.equal(f.counts().posts, 0)
})

test('a second send while running is ignored and stop failures stay visible', async () => {
  const f = fixture()
  await f.observer.reload()
  const sending = f.observer.send('问题', options)
  await f.observer.send('重复问题', options)
  assert.equal(f.counts().posts, 1)
  f.transport.stopChat = async () => { throw new Error('网络不可用') }
  await f.observer.stop()
  assert.match(f.observer.getSnapshot().error!, /停止失败/)
  f.emit({ type: 'delta', text: '仍在回答' })
  assert.match(f.observer.getSnapshot().error!, /停止失败/)
  f.observer.disconnect()
  f.stream.resolve()
  await sending
})

test('late optional context cannot overwrite the live run', async () => {
  const f = fixture()
  await f.observer.reload()
  const oldContext = deferred<null>()
  f.transport.getChatContext = () => oldContext.promise
  const refreshing = f.observer.refreshContext(true)
  const sending = f.observer.send('问题', options)
  const context = { thread_id: 't', turn_id: 'u', model: 'fake', context_window: 100, last: { inputTokens: 1, cachedInputTokens: 0, outputTokens: 2, reasoningOutputTokens: 0, totalTokens: 3 }, usage: {}, updated_at: user.created_at }
  f.emit({ type: 'context', context })
  oldContext.resolve(null)
  await refreshing
  assert.deepEqual(f.observer.getSnapshot().context, context)
  f.emit({ type: 'done', message })
  f.stream.resolve()
  await sending
})

test('interaction resolution does not dismiss a newer request', async () => {
  const f = fixture()
  await f.observer.reload()
  const sending = f.observer.send('问题', options)
  const interaction = { id: 'approval-2', method: 'item/commandExecution/requestApproval', params: {} }
  f.emit({ type: 'interaction', interaction })
  f.observer.resolveInteraction('approval-1')
  assert.deepEqual(f.observer.getSnapshot().pending?.interaction, interaction)
  f.observer.resolveInteraction('approval-2')
  assert.equal(f.observer.getSnapshot().pending?.interaction, null)
  f.emit({ type: 'done', message })
  f.stream.resolve()
  await sending
})

test('running snapshots and events preserve public progress, commentary, interaction and context', async () => {
  const live = fixture()
  await live.observer.reload()
  const sending = live.observer.send('问题', options)
  const interaction = { id: 'question', method: 'item/tool/requestUserInput', params: {} }
  const context = { thread_id: 't', turn_id: 'u', model: 'fake', context_window: 100, last: { inputTokens: 1, cachedInputTokens: 0, outputTokens: 2, reasoningOutputTokens: 0, totalTokens: 3 }, usage: {}, updated_at: user.created_at }
  for (const event of [
    { type: 'progress', text: '读取课件' }, { type: 'activity', text: '查看第 5 页' },
    { type: 'commentary', text: '核对', append: false }, { type: 'commentary', text: '公式', append: true },
    { type: 'interaction', interaction }, { type: 'context', context }, { type: 'compaction', active: true },
  ] satisfies ChatEvent[]) live.emit(event)
  const restored = fixture()
  const snapshot: ChatRun = { ...done, status: 'running', message: null, context, context_current: true, compacting: true,
    pending: { phase: 'start', expanded: null, text: '', corrected: false, activity: ['查看第 5 页'], progress: '读取课件', commentary: '核对公式', interaction } }
  restored.transport.getChatRun = async () => snapshot
  await restored.observer.reload()
  for (const key of ['pending', 'context', 'contextCurrent', 'compacting'] as const) {
    assert.deepEqual(restored.observer.getSnapshot()[key], live.observer.getSnapshot()[key])
  }
  live.observer.disconnect(); restored.observer.disconnect()
  live.stream.resolve()
  await sending
})

test('local failed retry removes the failure and preserves the original options', async () => {
  const f = fixture()
  await f.observer.reload()
  const first = f.observer.send('问题', options)
  f.emit({ type: 'error', error: '暂时失败' })
  f.stream.resolve()
  await first
  const failure = f.observer.getSnapshot().messages.at(-1)!
  let retryOptions: Parameters<ChatTransport['askStream']>[4]
  f.transport.askStream = async (_v, _q, emit, _signal, opts) => { retryOptions = opts; emit({ type: 'done', message }) }
  await f.observer.send(failure.question!, failure.options!, failure.id)
  assert.equal(f.observer.getSnapshot().messages.length, 2)
  assert.equal(f.observer.getSnapshot().messages.at(-1)?.status, 'ok')
  assert.ok(retryOptions)
  assert.deepEqual(retryOptions.note_target, options.note_target)
})

test('restoring a terminal snapshot invalidates an older optional context read', async () => {
  const f = fixture()
  const oldContext = deferred<null>()
  const context = { thread_id: 't', turn_id: 'u', model: 'fake', context_window: 100, last: { inputTokens: 1, cachedInputTokens: 0, outputTokens: 2, reasoningOutputTokens: 0, totalTokens: 3 }, usage: {}, updated_at: user.created_at }
  f.transport.getChatContext = () => oldContext.promise
  f.transport.getChatRun = async () => ({ ...done, context, context_current: true })
  const loading = f.observer.reload()
  const refreshing = f.observer.refreshContext(true)
  await loading
  oldContext.resolve(null)
  await refreshing
  assert.deepEqual(f.observer.getSnapshot().context, context)
  assert.equal(f.observer.getSnapshot().contextCurrent, true)
})
