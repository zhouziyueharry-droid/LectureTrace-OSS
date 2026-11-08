import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState, type KeyboardEvent, type ReactNode } from 'react'
import { commitOutline, createChatConversation, deleteChatConversation, errorText, formatDateTime, getCodexSettings, getStepwiseQaSettings, renameChatConversation, selectChatConversation, undoOutline, type ChatConversations, type ChatExpanded, type StepwiseQaSettings } from '../api.ts'
import { useChatObservation } from '../chat/useChatObservation.ts'
import type { LocalMessage } from '../chat/observation.ts'
import CitedMarkdown from './CitedMarkdown.tsx'
import UsageMeter from './UsageMeter.tsx'
import { ConfirmDialog } from './Modal.tsx'
import Splitter from './Splitter.tsx'
import { quoteQuestion, type OutlineQuote } from '../outlineQuote.ts'
import type { NoteTarget, PdfTarget } from '../api.ts'
import AgentInteraction from './AgentInteraction.tsx'
import { getChatModel, saveChatModel, getCodexModels, type ChatModel, type CodexModel } from '../api.ts'

interface Props {
  videoId: string
  enabled: boolean
  disabledHint: string
  onSeek: (t: number) => void
  reference?: OutlineQuote | null
  onClearReference?: () => void
  onOutlineChanged?: () => Promise<void>
  outlineRevision?: string
  /** 随提问附带的页面（显示名）；只有 Codex Agent 能读写页面 */
  noteAttach?: string | null
  pdfTarget?: PdfTarget
  onDetachNote?: () => void
  /** 提问前先把页面草稿写出去，并返回最新版本 */
  resolveNoteTarget?: () => Promise<NoteTarget | null>
  /** 页面里就地唤起 Agent：send 为 false 时只把内容放进输入框 */
  agentRequest?: { id: number; question: string; send: boolean } | null
  onNotesChanged?: () => void
  /** 标题栏右侧的额外按钮（学习页用来切换 Agent 的位置） */
  headExtra?: ReactNode
}

const EMPTY: never[] = []
const INPUT_H_MIN = 56
const INPUT_H_MAX = 320
const INPUT_H_KEY = 'lt.chatInputH.v1'

function loadInputH(): number {
  try {
    const v = Number(localStorage.getItem(INPUT_H_KEY))
    if (Number.isFinite(v) && v >= INPUT_H_MIN) return Math.min(INPUT_H_MAX, v)
  } catch {
    /* 存储不可用 */
  }
  return INPUT_H_MIN
}


function expandedText(e: ChatExpanded | null | undefined): string | null {
  if (!e) return null
  if (!e.t && !e.images) return '短索引已足够，未展开原文'
  const parts = []
  if (e.t) parts.push(`${e.t} 段讲解`)
  if (e.images) parts.push(`${e.images} 张截图`)
  return `已展开 ${parts.join('、')}`
}

export default function ChatPanel({ videoId, enabled, disabledHint, onSeek, reference, onClearReference, onOutlineChanged, outlineRevision, noteAttach, pdfTarget, onDetachNote, resolveNoteTarget, agentRequest, onNotesChanged, headExtra }: Props) {
  const inputId = useId()
  const { observer, messages, pending, sessions, loading, loadError, context, contextCurrent, compacting, preview, stopping, error: observationError } = useChatObservation(videoId, onNotesChanged)
  const [input, setInput] = useState('')
  const [inputH, setInputH] = useState(loadInputH)
  useEffect(() => {
    try {
      localStorage.setItem(INPUT_H_KEY, String(inputH))
    } catch {
      /* 存储不可用 */
    }
  }, [inputH])
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [deleteId, setDeleteId] = useState<string | null>(null)
  const [pickerOpen, setPickerOpen] = useState(false)
  const [pickerQuery, setPickerQuery] = useState('')
  const [blankHint, setBlankHint] = useState(false)
  const [copiedId, setCopiedId] = useState<LocalMessage['id'] | null>(null)
  const [sessionBusy, setSessionBusy] = useState(false)
  const [sessionError, setSessionError] = useState<string | null>(null)
  const [renamingId, setRenamingId] = useState<string | null>(null)
  const [renameText, setRenameText] = useState('')
  const [provider, setProvider] = useState<'api' | 'codex' | 'stepwise_qa'>('api')
  const [providerReady, setProviderReady] = useState(false)
  const [stepwiseModel, setStepwiseModel] = useState<StepwiseQaSettings | null>(null)
  const [model, setModel] = useState<ChatModel | null>(null)
  const [models, setModels] = useState<CodexModel[]>([])
  const [modelBusy, setModelBusy] = useState(false)
  const [modelError, setModelError] = useState<string | null>(null)
  const [preparing, setPreparing] = useState(false)
  const starting = useRef(false)
  const [allowEdit, setAllowEdit] = useState(false)
  const [undoRevision, setUndoRevision] = useState<string | null>(null)
  const [editBusy, setEditBusy] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)
  const modelRequestRef = useRef(0)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const pickerRef = useRef<HTMLDivElement>(null)
  const listRef = useRef<HTMLDivElement>(null)
  const stickRef = useRef(true)

  useEffect(() => {
    setRenamingId(null)
    setPickerOpen(false)
  }, [videoId])

  // 点击弹层以外的地方收起对话列表
  useEffect(() => {
    if (!pickerOpen) return
    const onDown = (e: MouseEvent) => {
      if (pickerRef.current && !pickerRef.current.contains(e.target as Node)) {
        setPickerOpen(false)
        setRenamingId(null)
      }
    }
    document.addEventListener('mousedown', onDown)
    return () => document.removeEventListener('mousedown', onDown)
  }, [pickerOpen])

  useEffect(() => {
    if (!blankHint) return
    const t = window.setTimeout(() => setBlankHint(false), 2600)
    return () => window.clearTimeout(t)
  }, [blankHint])

  useEffect(() => {
    let active = true
    getCodexSettings().then((s) => { if (active) { setProvider(s.provider); setProviderReady(true) } }, (e) => { if (active) { setActionError(errorText(e)); setProviderReady(true) } })
    return () => { active = false }
  }, [])

  useEffect(() => {
    let active = true
    if (provider !== 'stepwise_qa') {
      setStepwiseModel(null)
      return () => { active = false }
    }
    getStepwiseQaSettings().then((settings) => { if (active) setStepwiseModel(settings) }, () => {
      if (active) setStepwiseModel(null)
    })
    return () => { active = false }
  }, [provider])

  useEffect(() => {
    if (reference) inputRef.current?.focus()
  }, [reference])

  useEffect(() => {
    void observer.refreshContext(provider === 'codex')
  }, [observer, provider, outlineRevision, sessions?.current_id])

  // 新内容出现时，如果用户停留在底部，则自动滚到底
  useLayoutEffect(() => {
    const el = listRef.current
    if (el && stickRef.current) el.scrollTop = el.scrollHeight
  }, [messages, pending])

  const onScroll = () => {
    const el = listRef.current
    if (el) stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40
  }

  const busy = pending !== null || preparing

  const loadModels = useCallback(async () => {
    const requestId = ++modelRequestRef.current
    try {
      const [result, selection] = await Promise.all([getCodexModels(), getChatModel(videoId)])
      if (requestId !== modelRequestRef.current) return
      setModels(result.models)
      setModel(selection)
      setModelError(result.error)
    } catch (error) { if (requestId === modelRequestRef.current) setModelError(errorText(error)) }
  }, [videoId])

  useEffect(() => {
    setModel(null)
    if (provider === 'codex' && sessions?.current_id) void loadModels()
    return () => { modelRequestRef.current++ }
  }, [sessions?.current_id, provider, loadModels])

  const changeModel = async (selection: ChatModel) => {
    setModelBusy(true)
    setModelError(null)
    try { setModel(await saveChatModel(videoId, selection)) }
    catch (error) { setModelError(errorText(error)) }
    finally { setModelBusy(false) }
  }

  /** 提问；retryOf 为要重试的失败消息 id：移除它，且不重复添加同一个问题 */
  const ask = useCallback(
    async (q: string, retryOf?: LocalMessage['id'], retryOptions?: LocalMessage['options']) => {
      if (!q || observer.getSnapshot().pending || loading || sessionBusy || starting.current || modelBusy || !enabled) return
      const conversationId = sessions?.current_id
      starting.current = true
      setPreparing(true)
      let target: NoteTarget | null | undefined
      try {
        target = retryOptions ? retryOptions.note_target : noteAttach && !pdfTarget && provider === 'codex' ? await resolveNoteTarget?.() : undefined
      } catch (error) {
        setActionError(errorText(error))
        return
      } finally { starting.current = false; setPreparing(false) }
      if (observer.getSnapshot().sessions?.current_id !== conversationId) return
      const options = retryOptions ?? { node_id: reference?.node_id, allow_outline_edit: provider === 'codex' && allowEdit, note_target: target ?? undefined, pdf_target: pdfTarget }
      if ((options.note_target || options.pdf_target) && provider !== 'codex') {
        setActionError('让 Agent 读写页面需要在设置中切换到 Codex Agent')
        return
      }
      stickRef.current = true
      setActionError(null)
      setAllowEdit(false)
      onClearReference?.()
      await observer.send(q, options, retryOf)
    },
    [observer, enabled, loading, sessionBusy, sessions?.current_id, reference, allowEdit, provider, modelBusy, onClearReference, noteAttach, pdfTarget, resolveNoteTarget],
  )

  // 页面里就地唤起：发送时一定附带当前页面，不受输入框旁「随提问附带」开关影响
  const handledRequest = useRef(0)
  useEffect(() => {
    if (!agentRequest || !providerReady || (agentRequest.send && loading) || handledRequest.current === agentRequest.id) return
    handledRequest.current = agentRequest.id
    if (!agentRequest.send) {
      setInput((old) => (old.trim() ? `${old}

${agentRequest.question}` : agentRequest.question))
      window.setTimeout(() => inputRef.current?.focus(), 0)
      return
    }
    if (provider !== 'codex') { setActionError('让 Agent 读写页面需要在设置中切换到 Codex Agent'); return }
    void (async () => {
      const target = await resolveNoteTarget?.()
      if (!target) { setActionError('页面还有没处理的冲突，请先处理后再叫 Agent'); return }
      await ask(agentRequest.question, undefined, { allow_outline_edit: false, note_target: target })
    })()
  }, [agentRequest, providerReady, provider, loading, resolveNoteTarget, ask])

  const send = () => {
    const q = input.trim()
    if (!q || busy || loading || sessionBusy || editBusy || modelBusy || !enabled) return
    setInput('')
    void ask(quoteQuestion(q, reference))
  }

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault()
      send()
    }
  }

  const changeSession = async (action: () => Promise<ChatConversations>) => {
    if (busy || sessionBusy || editBusy) return
    setSessionBusy(true)
    setSessionError(null)
    try {
      await action()
      setAllowEdit(false)
      onClearReference?.()
      setConfirmDelete(false)
      await observer.reload()
      setRenamingId(null)
      setPickerOpen(false)
    } catch (e) {
      setSessionError(errorText(e))
    } finally {
      setSessionBusy(false)
    }
  }

  const renameSession = async () => {
    if (!renamingId || busy || sessionBusy || editBusy) return
    const conversationId = sessions?.current_id
    const title = renameText.trim()
    if (!title || title.length > 80) {
      setSessionError('标题须为 1–80 个字符')
      return
    }
    setSessionBusy(true)
    setSessionError(null)
    try {
      await renameChatConversation(videoId, renamingId, title)
      if (conversationId === observer.getSnapshot().sessions?.current_id) {
        await observer.refreshSessions()
        setRenamingId(null)
      }
    } catch (e) {
      if (conversationId === observer.getSnapshot().sessions?.current_id) setSessionError(errorText(e))
    } finally {
      setSessionBusy(false)
    }
  }

  const applyEdit = async (undo: boolean) => {
    if (editBusy || busy) return
    setEditBusy(true)
    setActionError(null)
    try {
      const result = undo && undoRevision ? await undoOutline(videoId, undoRevision)
        : preview ? await commitOutline(videoId, preview.proposal_id) : null
      if (!result) return
      setUndoRevision(undo ? null : result.revision ?? null)
      observer.dismissPreview()
      onClearReference?.()
      await onOutlineChanged?.()
    } catch (e) { setActionError(errorText(e)) } finally { setEditBusy(false) }
  }

  /** 失败消息对应的问题：本地消息自带，历史消息取它前面最近的一条用户消息 */
  const questionOf = (i: number): string | null => {
    const m = messages[i]
    if (m.question) return m.question
    for (let j = i - 1; j >= 0; j--) if (messages[j].role === 'user') return messages[j].content
    return null
  }

  const copy = async (m: LocalMessage) => {
    try {
      await navigator.clipboard.writeText(m.content)
      setCopiedId(m.id)
      window.setTimeout(() => setCopiedId((id) => (id === m.id ? null : id)), 1500)
    } catch {
      setActionError('复制失败：浏览器不允许访问剪贴板')
    }
  }

  const renderAssistant = (m: LocalMessage, i: number) => {
    const failed = m.status === 'failed'
    const note = expandedText(m.expanded)
    const q = failed ? questionOf(i) : null
    const learningQuestion = m.options?.learning_parent_question || m.question || questionOf(i) || ''
    return (
      <div className="ag-msg">
        <div className="ag-msg-head">
          <AgentGlyph size={22} />
          <strong>Agent</strong>
          <span className="muted small">{clockText(m.created_at)}</span>
        </div>
        <div className={`msg-bubble ag-body${failed ? ' is-failed' : ''}${m.stale ? ' is-stale' : ''}`}>
          {m.stale && <div className="msg-note">基于旧版本证据，时间戳已停用</div>}
          {!m.stale && (note || m.corrected) && (
            <div className="msg-note">
              {[note, m.corrected ? (failed ? '已尝试自动修正引用' : '引用已自动修正') : null].filter(Boolean).join(' · ')}
            </div>
          )}
          {m.content && (
            <CitedMarkdown
              content={m.content}
              citations={m.stale ? EMPTY : (m.citations ?? EMPTY)}
              onSeek={onSeek}
              inactive={m.stale ? 'stale' : undefined}
            />
          )}
          {failed && (
            <div className="msg-error small">
              <span>{m.error || '这条回答没有完成（引用校验失败或中断），未作为正常回答保存。'}</span>
              {q && (
                <button className="btn btn-small" onClick={() => void ask(q, m.id, m.options)} disabled={busy || editBusy || !enabled}>
                  重试
                </button>
              )}
            </div>
          )}
          {m.usage && <Usage usage={m.usage} />}
        </div>
        {provider === 'stepwise_qa' && !failed && !m.stale && (m.learning_options?.length ?? 0) > 0 && (
          <div className="ag-learning-options" aria-label="继续拓展">
            <span className="muted small">想继续了解？</span>
            {m.learning_options!.map((option) => (
              <button key={option.id} className="btn btn-small" disabled={busy || editBusy || !enabled}
                onClick={() => void ask(option.label, undefined, {
                  node_id: m.options?.node_id,
                  learning_action: option.id, learning_prompt: option.prompt, learning_scope: option.scope,
                  learning_parent_question: option.parent_question || learningQuestion,
                })}>
                <span className={`ag-learning-scope is-${option.scope}`}>{option.scope === 'course' ? '课程' : '课外补充'}</span>
                {' '}
                {option.label}
              </button>
            ))}
          </div>
        )}
        {!failed && m.content && (
          <div className="ag-actions">
            <button className="ag-ghost" onClick={() => void copy(m)}>{copiedId === m.id ? '已复制' : '复制'}</button>
          </div>
        )}
      </div>
    )
  }

  // 回答进行中的分步状态
  const steps: { state: 'done' | 'now'; text: string }[] = []
  if (pending && provider !== 'codex') {
    steps.push({ state: pending.phase === 'answer' ? 'done' : 'now', text: '定位相关片段' })
    if (pending.phase === 'answer') {
      const ex = expandedText(pending.expanded)
      if (ex) steps.push({ state: 'done', text: ex })
      steps.push({ state: 'now', text: pending.text ? '撰写回答' : '撰写回答…' })
    }
  }

  // 当前对话还没有任何消息：再点「新建」没有意义，后端也会复用这段空对话
  const currentSession = sessions?.conversations.find((s) => s.id === sessions.current_id)
  const currentIsBlank = !loading && messages.length === 0 && !currentSession?.title
  const currentTitle = currentSession?.title || '新对话'
  const locked = busy || sessionBusy || editBusy
  const query = pickerQuery.trim().toLowerCase()
  const listed = (sessions?.conversations ?? []).filter((s) => !query || (s.title || '新对话').toLowerCase().includes(query))
  const todayKey = new Date().toDateString()
  const groups = [
    { label: '今天', items: listed.filter((s) => new Date(s.updated_at).toDateString() === todayKey) },
    { label: '更早', items: listed.filter((s) => new Date(s.updated_at).toDateString() !== todayKey) },
  ].filter((g) => g.items.length > 0)

  const suggestions = [
    reference ? `总结「${reference.title}」这一节` : '总结这节课的主要内容',
    '这节课有哪些容易混淆的概念？',
    '出 3 道自测题，并附答案出处',
  ]

  const onNewChat = () => {
    if (currentIsBlank) {
      setBlankHint(true)
      inputRef.current?.focus()
      return
    }
    void changeSession(() => createChatConversation(videoId))
  }

  return (
    <section className="panel chat-panel" aria-label="步进式问答 Agent">
      <div className="ag-head">
        <div className="ag-title">
          <AgentGlyph size={24} />
          <h2>Agent</h2>
          <span className="ag-tag">{provider === 'codex' ? 'Codex' : provider === 'stepwise_qa' ? '步进式问答 Agent' : 'API'}</span>
          {provider === 'stepwise_qa' && stepwiseModel && <span className="ag-tag" title={`API 地址：${stepwiseModel.base_url}`}>
            {stepwiseModel.provider === 'custom' ? '自定义 API' : `${stepwiseModel.provider === 'deepseek' ? 'DeepSeek' : 'OpenAI'} API`}
            {' · '}{stepwiseModel.model}{' · '}{stepwiseModel.base_url.replace(/^https?:\/\//, '')}
          </span>}
          <span className="spacer" />
          {undoRevision && <button className="btn btn-small btn-text" disabled={busy || editBusy || undoRevision !== outlineRevision} onClick={() => void applyEdit(true)}>撤销大纲修改</button>}
          {headExtra}
        </div>
        <div className="ag-session" ref={pickerRef} onKeyDown={(e) => {
          if (e.key === 'Escape' && pickerOpen) {
            e.preventDefault()
            setRenamingId(null)
            setPickerOpen(false)
          }
        }}>
          <button className={`ag-switch-btn${pickerOpen ? ' is-open' : ''}`} type="button" aria-haspopup="listbox" aria-expanded={pickerOpen} title={currentTitle}
            onClick={() => { setPickerOpen((v) => !v); setPickerQuery('') }}>
            <span className={`ag-switch-title ellipsis${currentIsBlank ? ' is-blank' : ''}`}>{currentTitle}</span>
            <span className="muted small">{sessions ? `${sessions.conversations.length} 个对话` : ''}</span>
            <span className="ag-caret" aria-hidden />
          </button>
          <button className="btn ag-new" type="button" aria-disabled={currentIsBlank || undefined} disabled={locked || loading}
            title={currentIsBlank ? '当前已经是新对话，直接提问即可' : '开始一段新对话'} onClick={onNewChat}>
            ＋ 新对话
          </button>
          {blankHint && <div className="ag-tip" role="status">已经是新对话，直接提问即可</div>}
          {pickerOpen && (
            <div className="ag-pop" role="listbox" aria-label="本视频的对话">
              {(sessions?.conversations.length ?? 0) > 5 && (
                <input className="ag-pop-search" aria-label="搜索对话" placeholder="搜索对话" value={pickerQuery} autoFocus onChange={(e) => setPickerQuery(e.target.value)} />
              )}
              {sessionError && <p className="error-text small" role="alert">{sessionError}</p>}
              {groups.length === 0 && <p className="muted small ag-pop-empty">没有匹配的对话</p>}
              {groups.map((g) => (
                <div key={g.label} className="ag-pop-group">
                  <div className="ag-pop-label">{g.label}</div>
                  {g.items.map((s) => (
                    <div className={`ag-pop-row${s.id === sessions?.current_id ? ' is-current' : ''}`} key={s.id}>
                      {renamingId === s.id ? (
                        <form className="ag-rename" onSubmit={(e) => { e.preventDefault(); void renameSession() }}>
                          <input aria-label="对话标题" value={renameText} maxLength={80} autoFocus
                            onChange={(e) => setRenameText(e.target.value)} disabled={sessionBusy} />
                          <button className="btn btn-small" type="submit" disabled={sessionBusy}>保存</button>
                          <button className="btn btn-small btn-text" type="button" onClick={() => setRenamingId(null)} disabled={sessionBusy}>取消</button>
                        </form>
                      ) : (
                        <>
                          <button className="ag-pop-choice" type="button" role="option" aria-selected={s.id === sessions?.current_id}
                            title={`${s.title || '新对话'}\n${formatDateTime(s.updated_at)}`} disabled={locked}
                            onClick={() => (s.id === sessions?.current_id ? setPickerOpen(false) : void changeSession(() => selectChatConversation(videoId, s.id)))}>
                            <span className="ag-pop-title ellipsis">{s.title || '新对话'}</span>
                            <span className="muted small">{clockText(s.updated_at)}</span>
                          </button>
                          <button className="ag-icon" type="button" aria-label={`重命名“${s.title || '新对话'}”`} title="重命名" disabled={locked}
                            onClick={() => { setSessionError(null); setRenamingId(s.id); setRenameText(s.title || '') }}>
                            <Icon d="M3 13l.7-3L10.8 3l2.2 2.2L5.9 12.3zM9.5 4.5l2 2" />
                          </button>
                          <button className="ag-icon is-danger" type="button" aria-label={`删除“${s.title || '新对话'}”`} title="删除" disabled={locked}
                            onClick={() => { setSessionError(null); setDeleteId(s.id); setConfirmDelete(true) }}>
                            <Icon d="M3 4.5h10M6.5 4.5V3h3v1.5M4.5 4.5l.6 8h5.8l.6-8" />
                          </button>
                        </>
                      )}
                    </div>
                  ))}
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      <div className="chat-list panel-body" ref={listRef} onScroll={onScroll} aria-live="polite">
        {loading && <p className="muted">加载中…</p>}
        {loadError && <p className="error-text small">加载对话失败：{loadError}</p>}
        {sessionError && !pickerOpen && <p className="error-text small" role="alert">{sessionError}</p>}
        {!loading && !loadError && messages.length === 0 && !busy && (
          <div className="ag-empty">
            <AgentGlyph size={40} />
            <div>
              <strong>问这节课的任何问题</strong>
              <p className="muted small">回答会附带可跳转的原片段，并标明是否已核对原文。</p>
            </div>
            <div className="ag-suggest">
              <span className="muted small">试试这些</span>
              {suggestions.map((t) => (
                <button key={t} className="ag-suggest-item" disabled={!enabled || editBusy} onClick={() => void ask(t)}>
                  <span>{t}</span>
                  <span aria-hidden>›</span>
                </button>
              ))}
            </div>
          </div>
        )}
        {messages.map((m, i) => (
          <div key={m.id} className={`msg msg-${m.role}`}>
            {m.role === 'user' ? <div className="msg-bubble">{m.content}</div> : renderAssistant(m, i)}
          </div>
        ))}
        {pending && (
          <div className="msg msg-assistant">
            <div className="ag-msg">
              <div className="ag-msg-head">
                <AgentGlyph size={22} />
                <strong>Agent</strong>
                <span className="muted small">{pending.interaction ? '等待回复' : '正在回答'}</span>
              </div>
              {provider !== 'api' ? <div className="ag-live-progress" role="status">
                <span className="ag-live-dot" aria-hidden />
                {pending.text ? '正在生成回答' : pending.progress ?? '已发送，正在连接'}
              </div> : <ol className="ag-steps" aria-label="回答进度">
                {steps.map((s) => (
                  <li key={s.text} className={`is-${s.state}`}>
                    <span className="ag-step-mark" aria-hidden />
                    {s.text}
                  </li>
                ))}
              </ol>}
              {pending.commentary && <div className="ag-commentary"><CitedMarkdown content={pending.commentary} citations={EMPTY} onSeek={onSeek} inactive="pending" /></div>}
              {pending.interaction && <AgentInteraction key={pending.interaction.id} videoId={videoId} request={pending.interaction}
                onResolved={() => observer.resolveInteraction(pending.interaction!.id)} />}
              {!!pending.note_updates && <p className="msg-note" role="status">笔记操作已返回，可在页面查看最新内容或待接受的修改建议。</p>}
              {pending.corrected && <div className="msg-note">引用已自动修正</div>}
              {pending.activity.length > 0 && <details className="chat-activity"><summary>查阅过程</summary><ul>{pending.activity.map((text, i) => <li key={i}>{text}</li>)}</ul></details>}
              <div className="msg-bubble ag-body">
                {pending.text ? (
                  <CitedMarkdown content={pending.text} citations={EMPTY} onSeek={onSeek} inactive="pending" />
                ) : !pending.interaction ? (
                  <span className="typing" aria-label="正在思考">
                    <i />
                    <i />
                    <i />
                  </span>
                ) : null}
                {pending.usage && <Usage usage={pending.usage} />}
              </div>
            </div>
          </div>
        )}
        {preview && (
          <section className="outline-preview" aria-label="大纲修改预览">
            <h3>大纲修改预览</h3>
            <p className="muted small">确认下列差异后，点击提交才会更新大纲。</p>
            <pre aria-label="大纲差异">{preview.diff || '无文字差异'}</pre>
            <div className="settings-actions">
              <button className="btn btn-primary btn-small" disabled={busy || editBusy} onClick={() => void applyEdit(false)}>{editBusy ? '处理中…' : '提交大纲修改'}</button>
              <button className="btn btn-small" disabled={busy || editBusy} onClick={() => observer.dismissPreview()}>放弃修改</button>
            </div>
          </section>
        )}
      </div>

      <Splitter direction="row" sign={-1} label="拖拽调整输入框高度" controls={inputId} value={inputH} min={INPUT_H_MIN} max={INPUT_H_MAX} onStart={() => inputRef.current?.offsetHeight ?? INPUT_H_MIN} onResize={(h) => setInputH(Math.max(INPUT_H_MIN, Math.min(INPUT_H_MAX, h)))} onReset={() => setInputH(INPUT_H_MIN)} />
      <div className="chat-input ag-composer">
        {!enabled && <p className="muted small chat-hint">{disabledHint}</p>}
        {preparing && <p className="muted small chat-hint" role="status">正在保存页面并准备发送…</p>}
        {(actionError || observationError) && <p className="error-text small" role="alert">{actionError || observationError}</p>}
        {noteAttach && provider === 'codex' && <div className="chat-reference small">
          <div className="chat-reference-copy"><span>{pdfTarget ? 'PDF' : '页面'} · {noteAttach}</span><p>{pdfTarget ? 'Agent 可按页读文字、看图和局部放大；OCR 在本机运行，原件只读。' : '随提问附带，Agent 可读取它并以建议的形式提出修改。'}</p></div>
          <button className="btn btn-small btn-text" onClick={onDetachNote} aria-label="不再附带当前页面">移除</button>
        </div>}
        {reference && <div className="chat-reference small">
          <div className="chat-reference-copy"><span>大纲引用 · {reference.title}</span><p>{reference.excerpt}</p></div>
          <button className="btn btn-small btn-text" onClick={onClearReference} aria-label="移除大纲引用">移除</button>
        </div>}
        <div className="ag-box">
          <textarea
            id={inputId}
            ref={inputRef}
            aria-label="向 Agent 提问"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={onKeyDown}
            placeholder={enabled ? '问点什么，Enter 发送，Shift+Enter 换行' : '暂不可提问'}
            rows={2}
            style={{ minHeight: inputH, maxHeight: Math.max(160, inputH) }}
            disabled={!enabled || editBusy}
          />
          <div className="ag-box-foot">
            {provider === 'codex' && <div className="ag-model-controls">
              <select aria-label="当前对话模型" title="当前对话模型" value={model?.model ?? ''}
                disabled={busy || sessionBusy || modelBusy || !model || !models.length}
                onChange={event => {
                  const choice = models.find(item => item.id === event.target.value)
                  if (choice && model) void changeModel({ model: choice.id,
                    effort: choice.efforts.includes(model.effort) ? model.effort : choice.default_effort ?? choice.efforts[0] })
                }}>
                {!model && <option value="">加载模型…</option>}
                {model && !models.some(item => item.id === model.model) && <option value={model.model}>{model.model}</option>}
                {models.map(item => <option key={item.id} value={item.id}>{item.display_name}</option>)}
              </select>
              <select aria-label="推理强度" title="推理强度（reasoning effort）" value={model?.effort ?? ''}
                disabled={busy || sessionBusy || modelBusy || !model || !models.find(item => item.id === model.model)?.efforts.length}
                onChange={event => { if (model) void changeModel({ ...model, effort: event.target.value }) }}>
                {model && !models.find(item => item.id === model.model)?.efforts.includes(model.effort) && <option value={model.effort}>{model.effort}</option>}
                {models.find(item => item.id === model?.model)?.efforts.map(effort => <option key={effort} value={effort}>{effort}</option>)}
              </select>
            </div>}
            {provider === 'codex' && (
              <label className="ag-switch small">
                <input type="checkbox" role="switch" checked={allowEdit} disabled={busy || editBusy || !enabled} onChange={(e) => setAllowEdit(e.target.checked)} />
                <span className="ag-track" aria-hidden />
                允许修改大纲
              </label>
            )}
            <span className="spacer" />
            {provider === 'codex' && <UsageMeter snapshot={context} busy={busy} current={contextCurrent} compacting={compacting} />}
            {busy ? (
              <button className="btn" onClick={() => { setActionError(null); void observer.stop() }} disabled={stopping || preparing}>
                {preparing ? '准备中…' : stopping ? '停止中…' : '停止'}
              </button>
            ) : (
              <button className="btn btn-primary" onClick={send} disabled={!enabled || editBusy || modelBusy || !input.trim()}>
                发送
              </button>
            )}
          </div>
        </div>
        {modelError && provider === 'codex' && <p className="error-text small" role="alert">{modelError} <button className="ag-ghost" onClick={() => void loadModels()}>重新加载模型</button></p>}
        <p className="muted small chat-hint">{provider === 'codex' ? '消耗 ChatGPT 套餐额度' : provider === 'stepwise_qa' ? '步进式问答 Agent 使用独立模型设置' : '按当前接口计费'}</p>
      </div>

      <ConfirmDialog
        open={confirmDelete}
        title="删除对话"
        message={<p>确定删除{deleteId && deleteId !== sessions?.current_id ? '这段' : '当前'}对话的消息和笔记吗？删除后无法恢复，其他对话会保留。</p>}
        confirmText="删除"
        danger
        busy={sessionBusy}
        error={sessionError}
        onConfirm={() =>
          void changeSession(async () => {
            if (deleteId && deleteId !== sessions?.current_id) await selectChatConversation(videoId, deleteId)
            return deleteChatConversation(videoId)
          })
        }
        onCancel={() => !sessionBusy && (setConfirmDelete(false), setDeleteId(null))}
      />
    </section>
  )
}

function Icon({ d }: { d: string }) {
  return (
    <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d={d} />
    </svg>
  )
}

function AgentGlyph({ size }: { size: number }) {
  return (
    <span className="ag-glyph" style={{ width: size, height: size }} aria-hidden>
      <svg width={size * 0.6} height={size * 0.6} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <rect x="4" y="4" width="8" height="8" rx="1.5" />
        <path d="M6.5 1.5v2.5M9.5 1.5v2.5M6.5 12v2.5M9.5 12v2.5M1.5 6.5H4M1.5 9.5H4M12 6.5h2.5M12 9.5h2.5" />
      </svg>
    </span>
  )
}

/** 今天显示时分，更早显示月日 */
function clockText(iso: string | null | undefined): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  if (d.toDateString() === new Date().toDateString()) return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
  return `${d.getMonth() + 1}月${d.getDate()}日`
}

function Usage({ usage }: { usage: Record<string, unknown> }) {
  const fields = [
    ['input_tokens', '输入 token'], ['output_tokens', '输出 token'], ['reasoning_tokens', '推理 token'],
    ['cached_input_tokens', '缓存输入 token'], ['tool_calls', '读取调用'], ['read_tokens', '返回文字 token'], ['images', '截图'],
  ] as const
  return <details className="chat-usage small"><summary>本轮用量</summary><dl>{fields.map(([key, label]) => <div key={key}><dt>{label}</dt><dd>{typeof usage[key] === 'number' ? (usage[key] as number).toLocaleString() : '未知'}</dd></div>)}</dl></details>
}
