import { tr } from '../i18n.ts'
import { errorText, type ChatEvent, type ChatMessage, type ChatRun, type ChatConversations, type ContextSnapshot, type OutlinePreview } from '../api.ts'
import type * as api from '../api.ts'

export type ChatTransport = Pick<typeof api, 'askStream' | 'getChat' | 'getChatRun' | 'getChatContext' | 'listChatConversations' | 'stopChat'>
export type AskOptions = ChatRun['options']
export interface LocalMessage extends ChatMessage {
  question?: string
  expanded?: ChatRun['pending']['expanded']
  corrected?: boolean
  options?: AskOptions
  usage?: Record<string, unknown>
}
interface ObservationState {
  messages: LocalMessage[]
  pending: ChatRun['pending'] | null
  sessions: ChatConversations | null
  loading: boolean
  loadError: string | null
  error: string | null
  context: ContextSnapshot | null
  contextCurrent: boolean
  compacting: boolean
  preview: OutlinePreview | null
  stopping: boolean
}
interface Observation {
  controller: AbortController
  run: ChatRun | null
  notesSeen: number
  finished: boolean
  stopError: string | null
}

export function createChatObservation(videoId: string, transport: ChatTransport, onNotesChanged: () => void) {
  let state: ObservationState = { messages: [], pending: null, sessions: null, loading: true, loadError: null,
    error: null, context: null, contextCurrent: false, compacting: false, preview: null, stopping: false }
  let active: Observation | null = null
  let contextRequest = 0
  const listeners = new Set<() => void>()
  const update = (patch: Partial<ObservationState>) => {
    state = { ...state, ...patch }
    for (const listener of listeners) listener()
  }
  const current = (observation: Observation) => active === observation && !observation.controller.signal.aborted
  const disconnect = () => {
    active?.controller.abort()
    active = null
    contextRequest++
  }
  const begin = (): Observation => {
    disconnect()
    active = { controller: new AbortController(), run: null, notesSeen: 0, finished: false, stopError: null }
    return active
  }

  // Both transports end here: message metadata and side effects have one owner.
  const accept = (observation: Observation, run: ChatRun) => {
    if (!current(observation) || observation.finished) return
    contextRequest++
    observation.run = run
    const terminal = run.status !== 'running'
    const noteUpdates = run.pending.note_updates ?? 0
    const notifyNotes = noteUpdates > observation.notesSeen || terminal
    observation.notesSeen = Math.max(observation.notesSeen, noteUpdates)
    observation.finished = terminal
    let messages = state.messages
    if (terminal) {
      const last = messages.at(-1)
      const savedFailure = run.status === 'error' && last?.role === 'assistant' && last.status === 'failed' && last.created_at >= run.created_at ? last : null
      const message: LocalMessage = {
        ...(run.message ?? savedFailure ?? { id: `local-run-${run.id}`, role: 'assistant', content: run.pending.text,
          citations: [], created_at: run.created_at, status: 'failed', error: run.error ?? tr("回答已中断") }),
        question: run.question, options: { ...run.options, allow_outline_edit: !!run.options.allow_outline_edit }, expanded: run.pending.expanded,
        corrected: run.pending.corrected, usage: run.pending.usage,
      }
      messages = messages.some(m => m.id === message.id)
        ? messages.map(m => m.id === message.id ? message : m) : [...messages, message]
    }
    update({ messages, pending: terminal ? null : run.pending, context: run.status === 'error' ? null : run.context,
      contextCurrent: run.context_current && run.context !== null, compacting: !terminal && run.compacting,
      preview: run.status !== 'error' && run.options.allow_outline_edit ? run.preview : null, error: terminal ? null : observation.stopError })
    if (notifyNotes) onNotesChanged()
  }

  const receive = (observation: Observation, event: ChatEvent) => {
    if (!current(observation) || observation.finished || !observation.run) return
    const run = { ...observation.run, pending: { ...observation.run.pending } }
    const pending = run.pending
    switch (event.type) {
      case 'delta': pending.text += event.text; pending.phase = 'answer'; break
      case 'replace': pending.text = event.text; pending.corrected = true; break
      case 'phase': pending.phase = event.phase; pending.expanded = ('expanded' in event ? event.expanded : null) ?? null; break
      case 'activity': pending.activity = [...pending.activity, event.text]; break
      case 'progress': pending.progress = event.text; break
      case 'commentary': pending.commentary = (event.append ? pending.commentary ?? '' : '') + event.text; break
      case 'note_changed': pending.note_updates = (pending.note_updates ?? 0) + 1; break
      case 'interaction': pending.interaction = event.interaction; break
      case 'usage': pending.usage = event.usage; break
      case 'context': run.context = event.context; run.context_current = !event.previous; break
      case 'compaction': run.compacting = event.active; break
      case 'outline_preview': run.preview = event.preview; break
      case 'done': run.status = 'done'; run.message = event.message; break
      case 'error': run.status = 'error'; run.error = event.error || tr("回答失败"); break
    }
    accept(observation, run)
  }

  const pause = (signal: AbortSignal) => new Promise<void>(resolve => {
    const finish = () => { clearTimeout(timer); signal.removeEventListener('abort', finish); resolve() }
    const timer = setTimeout(finish, 1000)
    if (signal.aborted) finish()
    else signal.addEventListener('abort', finish, { once: true })
  })
  const recover = async (observation: Observation, initial?: ChatRun, failure = tr("连接意外结束，回答可能不完整")) => {
    if (initial) accept(observation, initial)
    while (current(observation) && !observation.finished) {
      if (initial) await pause(observation.controller.signal)
      if (!current(observation)) return
      try {
        const run = await transport.getChatRun(videoId, observation.run?.conversation_id, observation.controller.signal)
        if (!current(observation)) return
        if (!run || run.id !== observation.run?.id || run.conversation_id !== observation.run.conversation_id) {
          receive(observation, { type: 'error', error: initial ? tr("当前运行已变化，请重新加载对话查看最新状态。") : failure })
          return
        }
        accept(observation, run)
      } catch {
        if (current(observation)) update({ error: tr("连接中断，正在重新连接；不会重复发送问题。") })
      }
      initial = observation.run ?? undefined
    }
  }

  const refreshSessions = async () => {
    const observation = active
    const sessions = await transport.listChatConversations(videoId)
    if (observation && current(observation)) update({ sessions })
  }
  const reload = async () => {
    const observation = begin()
    update({ loading: true, loadError: null, messages: [], pending: null, context: null, contextCurrent: false,
      preview: null, compacting: false, stopping: false, error: null })
    try {
      const sessions = await transport.listChatConversations(videoId)
      if (!current(observation)) return
      update({ sessions })
      const messages = await transport.getChat(videoId)
      if (!current(observation)) return
      update({ messages: messages ?? [] })
      const run = await transport.getChatRun(videoId, sessions.current_id, observation.controller.signal)
      if (!current(observation)) return
      if (run && run.conversation_id === sessions.current_id) {
        if (!state.messages.some(m => m.role === 'user' && m.content === run.question && m.created_at >= run.created_at)) {
          update({ messages: [...state.messages, { id: `local-u-${run.id}`, role: 'user', content: run.question, citations: [], created_at: run.created_at }] })
        }
        void recover(observation, run)
      }
    } catch (error) {
      if (current(observation)) update({ loadError: errorText(error) })
    } finally {
      if (current(observation)) update({ loading: false })
    }
  }

  const send = async (question: string, options: AskOptions, retryOf?: ChatMessage['id']) => {
    if (!active || !question || state.loading || state.pending || state.stopping || !state.sessions) return
    const observation = begin()
    const run: ChatRun = { id: crypto.randomUUID(), conversation_id: state.sessions.current_id, question, options,
      created_at: new Date().toISOString(), status: 'running', context: state.context, context_current: false,
      compacting: false, preview: null, message: null, error: null,
      pending: { phase: 'start', expanded: null, text: '', corrected: false, activity: [] } }
    let messages = state.messages.filter(m => !(retryOf != null && String(retryOf).startsWith('local-') && m.id === retryOf))
    const last = messages.at(-1)
    if (!(retryOf != null && last?.role === 'user' && last.content === question)) {
      messages = [...messages, { id: `local-u-${run.id}`, role: 'user', content: question, citations: [], created_at: run.created_at }]
    }
    update({ messages })
    accept(observation, run)
    let failure = tr("连接意外结束，回答可能不完整")
    try {
      await transport.askStream(videoId, question, event => receive(observation, event), observation.controller.signal, { ...options, run_id: run.id })
    } catch (error) {
      // Losing a subscription is not evidence that the server stopped the run.
      failure = errorText(error)
    }
    if (!current(observation)) return
    if (!observation.finished) await recover(observation, undefined, failure)
    if (current(observation)) void refreshSessions().catch(() => {})
  }

  const stop = async () => {
    const observation = active
    if (!observation?.run || observation.finished || state.stopping) return
    observation.stopError = null
    update({ stopping: true, error: null })
    try {
      await transport.stopChat(videoId, observation.run.id)
      if (current(observation)) update({ context: null })
    } catch (error) {
      if (current(observation)) {
        observation.stopError = tr("停止失败：{0}", errorText(error))
        update({ error: observation.stopError })
      }
    } finally {
      if (current(observation)) update({ stopping: false })
    }
  }

  const refreshContext = async (enabled: boolean) => {
    const request = ++contextRequest
    if (state.pending) return
    update({ context: null, contextCurrent: false, compacting: false })
    if (!enabled) return
    try {
      const context = await transport.getChatContext(videoId)
      if (request === contextRequest && !state.pending) update({ context })
    } catch { /* Statistics must not block questions. */ }
  }

  return {
    getSnapshot: () => state,
    subscribe: (listener: () => void) => { listeners.add(listener); return () => { listeners.delete(listener) } },
    reload, send, stop, disconnect, refreshSessions, refreshContext,
    dismissPreview: () => update({ preview: null }),
    resolveInteraction: (id: string) => {
      if (active?.run?.pending.interaction?.id === id) {
        receive(active, { type: 'interaction', interaction: null })
      }
    },
  }
}
