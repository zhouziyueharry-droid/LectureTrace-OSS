import { tr, useLocale } from '../i18n.ts'
import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from 'react'
import type { Citation } from '../api.ts'
import CitedMarkdown from '../components/CitedMarkdown.tsx'
import CitationInput, { inputSelection, inputText, selectInput } from './CitationInput.tsx'
import { blockKind, clockToken, insideOpenFence, joinBlocks, listContinuation, parseBlocks, toggleTask } from './blocks.ts'
import { locate, type Hunk } from './suggestions.ts'

/** 就地叫 Agent：选中文字、块手柄、「/」菜单三个入口共用 */
export interface AgentAsk {
  where: 'selection' | 'block' | 'insert'
  quote: string
  /** 插入位置前面的一段，帮助 Agent 接上文 */
  before?: string
  instruction: string
  /** false：只把引用放进 Agent 输入框，让用户接着写 */
  send: boolean
}

interface Placement { id: string; at: number; covers: number; hunk: Hunk }

interface Props {
  text: string
  onChange: (text: string) => void
  citations: Citation[]
  stale: boolean
  onSeek: (t: number) => void
  getTime: () => number
  onAgent?: (ask: AgentAsk) => void
  onSave?: () => void
  /** Agent 建议：按「当前」块位置内联显示，被建议覆盖的块暂不可编辑 */
  suggestions?: Hunk[]
  renderSuggestion?: (hunk: Hunk) => ReactNode
  readOnly?: boolean
}

interface Block { id: number; text: string }

function GutterIcon({ kind }: { kind: 'plus' | 'grip' }) {
  useLocale()
  return (
    <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" aria-hidden>
      {kind === 'plus' ? <path d="M8 3.5v9M3.5 8h9" /> : (
        <g fill="currentColor" stroke="none">
          <circle cx="5.8" cy="3.8" r="1.1" /><circle cx="10.2" cy="3.8" r="1.1" />
          <circle cx="5.8" cy="8" r="1.1" /><circle cx="10.2" cy="8" r="1.1" />
          <circle cx="5.8" cy="12.2" r="1.1" /><circle cx="10.2" cy="12.2" r="1.1" />
        </g>
      )}
    </svg>
  )
}

type Caret = 'start' | 'end' | { from: number; to: number }
interface FocusRequest { id: number; caret: Caret }

let nextId = 1
const make = (text: string): Block => ({ id: nextId++, text })
const fromText = (text: string) => parseBlocks(text).map(make)
const texts = (blocks: Block[]) => blocks.map((b) => b.text)

interface SlashItem { key: string; label: string; hint: string; words: string; agent?: boolean }
const SLASH: SlashItem[] = [
  { key: 'h1', get label() { return tr("标题 1") }, hint: '#', words: 'h1 heading 标题' },
  { key: 'h2', get label() { return tr("标题 2") }, hint: '##', words: 'h2 heading 标题' },
  { key: 'h3', get label() { return tr("标题 3") }, hint: '###', words: 'h3 heading 标题' },
  { key: 'ul', get label() { return tr("项目符号") }, hint: '-', words: 'ul list bullet 列表 符号' },
  { key: 'ol', get label() { return tr("编号列表") }, hint: '1.', words: 'ol number 编号 列表' },
  { key: 'todo', get label() { return tr("待办") }, hint: '- [ ]', words: 'todo task check 待办 任务' },
  { key: 'quote', get label() { return tr("引用块") }, hint: '>', words: 'quote 引用' },
  { key: 'code', get label() { return tr("代码块") }, hint: '```', words: 'code 代码' },
  { key: 'math', get label() { return tr("公式块") }, hint: '$$', words: 'math latex formula 公式' },
  { key: 'table', get label() { return tr("表格") }, hint: '|', words: 'table 表格' },
  { key: 'rule', get label() { return tr("分割线") }, hint: '---', words: 'hr rule divider 分割线' },
  { key: 'clock', get label() { return tr("当前时间戳") }, hint: 'Ctrl+;', words: 'time clock timestamp 时间 时间戳' },
  { key: 'agent', get label() { return tr("让 Agent 写这一段") }, hint: '', words: 'agent ai 写 起草', agent: true },
]

const TEMPLATES: Record<string, { text: string; caret?: Caret }> = {
  h1: { text: '# ' }, h2: { text: '## ' }, h3: { text: '### ' }, ul: { text: '- ' }, ol: { text: '1. ' }, todo: { text: '- [ ] ' },
  quote: { text: '> ' }, code: { text: '```\n\n```', caret: { from: 4, to: 4 } }, math: { text: '$$\n\n$$', caret: { from: 3, to: 3 } },
  table: { get text() { return tr("| 列 1 | 列 2 |\n| --- | --- |\n|  |  |") }, get caret() { const at = this.text.lastIndexOf('\n') + 3; return { from: at, to: at } } }, rule: { text: '---' },
}

export default function BlockEditor({ text, onChange, citations, stale, onSeek, getTime, onAgent, onSave, suggestions = [], renderSuggestion, readOnly }: Props) {
  useLocale()
  const [blocks, setBlocks] = useState<Block[]>(() => fromText(text))
  const [active, setActive] = useState<number | null>(null)
  const [menuFor, setMenuFor] = useState<number | null>(null)
  const [promptFor, setPromptFor] = useState<{ id: number; quote: string; where: AgentAsk['where'] } | null>(null)
  const blocksRef = useRef(blocks)
  blocksRef.current = blocks
  const focusRequest = useRef<FocusRequest | null>(null)
  const rootRef = useRef<HTMLDivElement>(null)

  // 外部正文变化（刷新、合并、接受建议）时重新分块；自己产生的变化不重排，保留正在编辑的空块
  // 文本没变的块沿用原来的 id：别处更新页面时，正在编辑的那一块不会被踢出编辑态。
  useEffect(() => {
    const current = blocksRef.current
    if (joinBlocks(texts(current)) === text) return
    const pool = new Map<string, number[]>()
    for (const b of current) pool.set(b.text, [...(pool.get(b.text) ?? []), b.id])
    const next = parseBlocks(text).map((t) => ({ id: pool.get(t)?.shift() ?? nextId++, text: t }))
    blocksRef.current = next
    setBlocks(next)
    setActive((a) => (a !== null && next.some((b) => b.id === a) ? a : null))
  }, [text])

  // 建议按非空块定位，再换算回编辑器里的下标（编辑中可能夹着尚未输入的空块）
  const placements = useMemo(() => {
    if (!suggestions.length) return [] as Placement[]
    const filled = blocks.flatMap((b, idx) => (b.text.trim() ? [{ idx, text: b.text }] : []))
    const lines = filled.map((b) => b.text)
    return suggestions.flatMap((hunk): Placement[] => {
      const range = locate(lines, hunk)
      if (!range) return []
      const at = range.start < filled.length ? filled[range.start].idx : blocks.length
      const covers = range.end > range.start ? filled[range.end - 1].idx - at + 1 : 0
      return [{ id: hunk.id, at, covers, hunk }]
    })
  }, [suggestions, blocks])
  // 焦点离开一个空块时把它去掉（空块只是编辑中的占位，不会写进文件）
  const prevActive = useRef<number | null>(null)
  useEffect(() => {
    const prev = prevActive.current
    prevActive.current = active
    if (prev === null || prev === active) return
    const current = blocksRef.current
    const gone = current.find((b) => b.id === prev)
    if (gone && gone.text.trim() === '' && current.length > 1) {
      const next = current.filter((b) => b.id !== prev)
      blocksRef.current = next
      setBlocks(next)
    }
  }, [active])

  const hidden = useMemo(() => {
    const set = new Set<number>()
    for (const p of placements) for (let k = 0; k < p.covers; k++) set.add(p.at + k)
    return set
  }, [placements])

  const commit = useCallback((next: Block[], focus?: FocusRequest) => {
    blocksRef.current = next
    setBlocks(next)
    onChange(joinBlocks(texts(next)))
    if (focus) { focusRequest.current = focus; setActive(focus.id) }
  }, [onChange])

  useEffect(() => {
    const request = focusRequest.current
    if (!request) return
    const el = rootRef.current?.querySelector<HTMLElement>(`[data-block="${request.id}"]`)
    if (!el) return
    focusRequest.current = null
    el.focus({ preventScroll: false })
    const length = inputText(el).length
    const [from, to] = request.caret === 'start' ? [0, 0] : request.caret === 'end' ? [length, length] : [request.caret.from, request.caret.to]
    selectInput(el, from, to)
  })

  useEffect(() => {
    if (menuFor === null) return
    const dismiss = (event: PointerEvent) => { if (!(event.target instanceof Element) || !event.target.closest('.blk-menu, .blk-grip')) setMenuFor(null) }
    const escape = (event: globalThis.KeyboardEvent) => { if (event.key === 'Escape') setMenuFor(null) }
    document.addEventListener('pointerdown', dismiss)
    document.addEventListener('keydown', escape)
    return () => { document.removeEventListener('pointerdown', dismiss); document.removeEventListener('keydown', escape) }
  }, [menuFor])

  const indexOf = (id: number) => blocksRef.current.findIndex((b) => b.id === id)
  const setText = (id: number, value: string, caret?: Caret) => {
    const i = indexOf(id)
    if (i < 0) return
    const next = blocksRef.current.slice()
    next[i] = { id, text: value }
    commit(next, caret ? { id, caret } : undefined)
  }
  const insertAfter = (id: number, value = '') => {
    const i = indexOf(id)
    const block = make(value)
    const next = blocksRef.current.slice()
    next.splice(i + 1, 0, block)
    commit(next, { id: block.id, caret: 'end' })
  }
  const visible = (from: number, step: 1 | -1) => {
    let k = from + step
    while (k >= 0 && k < blocksRef.current.length && hidden.has(k)) k += step
    return k >= 0 && k < blocksRef.current.length ? k : -1
  }
  const remove = (id: number, focus: 'prev' | 'none' = 'prev') => {
    const i = indexOf(id)
    if (i < 0) return
    if (blocksRef.current.length === 1) { commit([make('')]); setActive(null); return }
    const next = blocksRef.current.filter((b) => b.id !== id)
    const prev = visible(i, -1)
    const target = focus === 'prev' && prev >= 0 ? blocksRef.current[prev] : null
    commit(next, target ? { id: target.id, caret: 'end' } : undefined)
    if (!target) setActive(null)
  }
  const move = (id: number, step: 1 | -1) => {
    const i = indexOf(id)
    const j = i + step
    if (i < 0 || j < 0 || j >= blocksRef.current.length) return
    const next = blocksRef.current.slice()
    ;[next[i], next[j]] = [next[j], next[i]]
    commit(next)
  }
  const duplicate = (id: number) => {
    const i = indexOf(id)
    const next = blocksRef.current.slice()
    next.splice(i + 1, 0, make(blocksRef.current[i].text))
    commit(next)
  }
  const deactivate = (id: number) => {
    setActive((cur) => (cur === id ? null : cur))
    const block = blocksRef.current.find((b) => b.id === id)
    if (block && block.text.trim() === '' && blocksRef.current.length > 1) remove(id, 'none')
  }
  const activate = (id: number, caret: Caret = 'end') => {
    if (readOnly) return
    focusRequest.current = { id, caret }
    setActive(id)
  }
  const goto = (i: number, caret: Caret) => { if (i >= 0) activate(blocksRef.current[i].id, caret) }

  const appendBlock = () => {
    const last = blocksRef.current[blocksRef.current.length - 1]
    if (last && last.text.trim() === '') activate(last.id)
    else { const block = make(''); commit([...blocksRef.current, block], { id: block.id, caret: 'end' }) }
  }

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>, block: Block, slash: SlashItem[] | null, slashIndex: number, setSlashIndex: (n: number) => void) => {
    const el = event.currentTarget
    const value = inputText(el)
    const { from: start, to: end } = inputSelection(el)
    const i = indexOf(block.id)
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') { event.preventDefault(); onSave?.(); return }
    if ((event.ctrlKey || event.metaKey) && event.key === ';') {
      event.preventDefault()
      const token = clockToken(getTime())
      setText(block.id, value.slice(0, start) + token + value.slice(end), { from: start + token.length, to: start + token.length })
      return
    }
    if (slash && slash.length) {
      if (event.key === 'ArrowDown') { event.preventDefault(); setSlashIndex((slashIndex + 1) % slash.length); return }
      if (event.key === 'ArrowUp') { event.preventDefault(); setSlashIndex((slashIndex - 1 + slash.length) % slash.length); return }
      if ((event.key === 'Enter' || event.key === 'Tab') && !event.nativeEvent.isComposing) { event.preventDefault(); applySlash(block, slash[slashIndex]); return }
    }
    if (event.key === 'Escape') { event.preventDefault(); if (promptFor) setPromptFor(null); else el.blur(); return }
    if (event.key === 'Enter' && !event.nativeEvent.isComposing) {
      if (event.ctrlKey || event.metaKey) { event.preventDefault(); el.blur(); return }
      if (event.shiftKey) return
      const kind = blockKind(value)
      if (kind === 'code' || kind === 'math' || insideOpenFence(value, start) || kind === 'table') return
      event.preventDefault()
      if (kind === 'list' || kind === 'quote') {
        const quote = kind === 'quote'
        const lineStart = value.lastIndexOf('\n', start - 1) + 1
        const line = value.slice(lineStart, value.indexOf('\n', start) === -1 ? value.length : value.indexOf('\n', start))
        const c = quote ? (/^>\s*$/.test(line) ? { end: true as const } : { prefix: '> ' }) : listContinuation(value, start)
        if (c && 'prefix' in c) {
          const insert = '\n' + c.prefix
          setText(block.id, value.slice(0, start) + insert + value.slice(end), { from: start + insert.length, to: start + insert.length })
          return
        }
        if (c && 'end' in c) {
          const kept = (value.slice(0, lineStart) + value.slice(lineStart + line.length).replace(/^\n/, '')).replace(/\n+$/, '')
          const next = blocksRef.current.slice()
          const fresh = make('')
          next[i] = { id: block.id, text: kept }
          next.splice(i + 1, 0, fresh)
          commit(next, { id: fresh.id, caret: 'start' })
          return
        }
      }
      const before = value.slice(0, start).trimEnd()
      const after = value.slice(end).trimStart()
      const fresh = make(after)
      const next = blocksRef.current.slice()
      next[i] = { id: block.id, text: before }
      next.splice(i + 1, 0, fresh)
      commit(next, { id: fresh.id, caret: 'start' })
      return
    }
    if (event.key === 'Backspace' && start === 0 && end === 0) {
      if (value === '') { event.preventDefault(); remove(block.id); return }
      const prev = visible(i, -1)
      if (prev >= 0 && blocksRef.current[prev].text.trim() === '') { event.preventDefault(); remove(blocksRef.current[prev].id, 'none'); activate(block.id, 'start') }
      return
    }
    if (event.key === 'ArrowUp' && start === end && !value.slice(0, start).includes('\n')) {
      const prev = visible(i, -1)
      if (prev >= 0) { event.preventDefault(); goto(prev, 'end') }
      return
    }
    if (event.key === 'ArrowDown' && start === end && !value.slice(end).includes('\n')) {
      const next = visible(i, 1)
      if (next >= 0) { event.preventDefault(); goto(next, 'start') }
    }
  }

  const applySlash = (block: Block, item: SlashItem) => {
    if (item.key === 'clock') {
      const token = clockToken(getTime()) + ' '
      setText(block.id, token, { from: token.length, to: token.length })
    } else if (item.key === 'agent') {
      setText(block.id, '', 'start')
      const i = indexOf(block.id)
      setPromptFor({ id: block.id, quote: blocksRef.current[i - 1]?.text ?? '', where: 'insert' })
    } else if (item.key === 'rule') {
      const fresh = make('')
      const next = blocksRef.current.slice()
      next[indexOf(block.id)] = { id: block.id, text: '---' }
      next.splice(indexOf(block.id) + 1, 0, fresh)
      commit(next, { id: fresh.id, caret: 'start' })
    } else {
      const template = TEMPLATES[item.key]
      setText(block.id, template.text, template.caret ?? 'end')
    }
  }

  const askAgent = (ask: AgentAsk) => { onAgent?.(ask); setPromptFor(null); setMenuFor(null); setActive(null) }
  const closePrompt = () => { if (promptFor) activate(promptFor.id); setPromptFor(null) }

  const rows: ReactNode[] = []
  const byAt = new Map<number, Placement[]>()
  for (const p of placements) byAt.set(p.at, [...(byAt.get(p.at) ?? []), p])
  blocks.forEach((block, i) => {
    for (const p of byAt.get(i) ?? []) rows.push(<div className="blk-placement" key={`p-${p.id}`}>{renderSuggestion?.(p.hunk)}</div>)
    if (hidden.has(i)) return
    rows.push(
      <BlockRow key={block.id} block={block} index={i} count={blocks.length} active={active === block.id} menuOpen={menuFor === block.id}
        prompt={promptFor?.id === block.id ? promptFor : null} readOnly={readOnly} citations={citations} stale={stale} onSeek={onSeek}
        agentEnabled={!!onAgent} first={blocks.length === 1 && block.text === ''}
        onActivate={(caret) => activate(block.id, caret)} onDeactivate={() => deactivate(block.id)}
        onText={(value, caret) => setText(block.id, value, caret)} onKeyDown={onKeyDown} onApplySlash={(item) => applySlash(block, item)}
        onPlus={() => insertAfter(block.id)} onMenu={() => setMenuFor(menuFor === block.id ? null : block.id)}
        onMove={(step) => { move(block.id, step); setMenuFor(null) }} onDuplicate={() => { duplicate(block.id); setMenuFor(null) }}
        onRemove={() => { remove(block.id, 'none'); setMenuFor(null) }} onAsk={askAgent}
        onOpenPrompt={(quote, where) => { setPromptFor({ id: block.id, quote, where }); setMenuFor(null) }}
        onClosePrompt={closePrompt} onToggleTask={(n) => setText(block.id, toggleTask(block.text, n))} />,
    )
  })
  for (const p of byAt.get(blocks.length) ?? []) rows.push(<div className="blk-placement" key={`p-${p.id}`}>{renderSuggestion?.(p.hunk)}</div>)

  return (
    <div className="blk-editor" ref={rootRef}>
      {rows}
      {!readOnly && <div className="blk-tail" onClick={appendBlock} aria-hidden />}
    </div>
  )
}

interface RowProps {
  block: Block
  index: number
  count: number
  active: boolean
  menuOpen: boolean
  prompt: { id: number; quote: string; where: AgentAsk['where'] } | null
  readOnly?: boolean
  citations: Citation[]
  stale: boolean
  onSeek: (t: number) => void
  agentEnabled: boolean
  first: boolean
  onActivate: (caret?: Caret) => void
  onDeactivate: () => void
  onText: (value: string, caret?: Caret) => void
  onKeyDown: (event: KeyboardEvent<HTMLDivElement>, block: Block, slash: SlashItem[] | null, slashIndex: number, setSlashIndex: (n: number) => void) => void
  onApplySlash: (item: SlashItem) => void
  onPlus: () => void
  onMenu: () => void
  onMove: (step: 1 | -1) => void
  onDuplicate: () => void
  onRemove: () => void
  onAsk: (ask: AgentAsk) => void
  onOpenPrompt: (quote: string, where: AgentAsk['where']) => void
  onClosePrompt: () => void
  onToggleTask: (n: number) => void
}

function BlockRow(props: RowProps) {
  const locale = useLocale()
  const { block, active, menuOpen, prompt, readOnly, citations, stale, onSeek, agentEnabled, first } = props
  const [selection, setSelection] = useState<[number, number]>([0, 0])
  const [slashIndex, setSlashIndex] = useState(0)
  const [promptText, setPromptText] = useState('')
  useEffect(() => { if (!active) setSelection([0, 0]) }, [active])
  useEffect(() => { if (prompt) setPromptText('') }, [prompt?.where, prompt?.id])

  const slashMatch = active && !block.text.includes('\n') ? /^\/([^\s/]{0,12})$/.exec(block.text) : null
  const slash = useMemo(() => {
    if (!slashMatch) return null
    const q = slashMatch[1].toLowerCase()
    const items = SLASH.filter((s) => (agentEnabled || !s.agent) && (!q || s.label.toLowerCase().includes(q) || s.words.includes(q)))
    return items.length ? items : null
  }, [slashMatch?.[1], agentEnabled, locale])
  useEffect(() => { setSlashIndex(0) }, [slashMatch?.[1]])

  const hasSelection = active && selection[1] > selection[0]
  const selected = hasSelection ? block.text.slice(selection[0], selection[1]) : ''
  const wrap = (left: string, right: string, link = false) => {
    const [from, to] = selection
    const body = block.text.slice(from, to)
    const insert = link ? `[${body}](https://)` : left + body + right
    const value = block.text.slice(0, from) + insert + block.text.slice(to)
    const caret = link ? { from: from + body.length + 3, to: from + body.length + 11 } : { from: from + left.length, to: from + left.length + body.length }
    props.onText(value, caret)
  }
  const submitPrompt = () => {
    if (!prompt || !promptText.trim()) return
    props.onAsk({ where: prompt.where, quote: prompt.where === 'insert' ? '' : prompt.quote, before: prompt.where === 'insert' ? prompt.quote : undefined, instruction: promptText.trim(), send: true })
  }
  const keep = (event: { preventDefault: () => void }) => event.preventDefault()

  const rendered = (
    <div className="blk-body" role="group" tabIndex={readOnly ? -1 : 0} aria-label={tr("段落，点击或按 Enter 编辑")}
      onClick={(event) => {
        const target = event.target as HTMLElement
        const task = target.closest('li.task-list-item')
        if (task && !readOnly) {
          const all = Array.from(event.currentTarget.querySelectorAll('li.task-list-item'))
          props.onToggleTask(all.indexOf(task))
          return
        }
        if (target.closest('button, a, .katex-display')) return
        if (!window.getSelection()?.isCollapsed) return
        props.onActivate('end')
      }}
      onKeyDown={(event) => { if (event.key === 'Enter' && event.target === event.currentTarget) { event.preventDefault(); props.onActivate('end') } }}>
      {block.text.trim() ? <CitedMarkdown content={block.text} citations={citations} onSeek={onSeek} inactive={stale ? 'stale' : undefined} />
        : <p className="blk-empty">{first ? tr("输入内容，按 / 插入块，或让 Agent 起草") : tr("空段落")}</p>}
    </div>
  )

  return (
    <div className={`blk${active ? ' is-active' : ''}${menuOpen ? ' has-menu' : ''}`}
      onBlur={(event) => { if (active && !event.currentTarget.contains(event.relatedTarget as Node | null)) props.onDeactivate() }}>
      {!readOnly && (
        <div className="blk-gutter">
          <button type="button" className="blk-grip" aria-label={tr("在下方添加段落")} title={tr("在下方添加段落")} onMouseDown={keep} onClick={props.onPlus}><GutterIcon kind="plus" /></button>
          <button type="button" className="blk-grip" aria-label={tr("段落菜单")} title={tr("拖动不可用；点开可上移、下移、复制、删除，或叫 Agent")} aria-expanded={menuOpen} onClick={props.onMenu}><GutterIcon kind="grip" /></button>
        </div>
      )}
      {active ? (
        <CitationInput id={block.id} className={`blk-input is-${blockKind(block.text)}${/^#{1,3}\s/.test(block.text) ? ` is-h${/^(#{1,3})/.exec(block.text)![1].length}` : ''}`} value={block.text} citations={citations}
          placeholder={first ? tr("输入内容，或按 / 插入块、让 Agent 起草") : tr("输入内容，按 / 插入块")}
          onChange={(value) => props.onText(value)}
          onSelect={({ from, to }) => setSelection([from, to])}
          onKeyDown={(event) => props.onKeyDown(event, block, slash, slashIndex, setSlashIndex)} />
      ) : rendered}
      {hasSelection && selected.trim() && (
        <div className="blk-bar" role="toolbar" aria-label={tr("选中文字工具")} onMouseDown={keep}>
          {agentEnabled && <>
            <button type="button" className="is-agent" onClick={() => props.onOpenPrompt(selected, 'selection')}>{tr("问 Agent")}</button>
            <button type="button" onClick={() => props.onAsk({ where: 'selection', quote: selected, instruction: tr("改写得更清晰简洁，保持原意和原有引用"), send: true })}>{tr("改写")}</button>
            <button type="button" onClick={() => props.onAsk({ where: 'selection', quote: selected, instruction: tr("把这段总结成几条要点，保留关键公式"), send: true })}>{tr("总结")}</button>
            <span className="blk-bar-sep" aria-hidden />
          </>}
          <button type="button" aria-label={tr("加粗")} onClick={() => wrap('**', '**')}><strong>B</strong></button>
          <button type="button" aria-label={tr("斜体")} onClick={() => wrap('*', '*')}><em>I</em></button>
          <button type="button" aria-label={tr("行内公式")} onClick={() => wrap('$', '$')}>∑</button>
          <button type="button" aria-label={tr("链接")} onClick={() => wrap('', '', true)}>{tr("链接")}</button>
        </div>
      )}
      {slash && (
        <div className="blk-slash" role="listbox" aria-label={tr("插入块")} onMouseDown={keep}>
          {slash.map((item, i) => (
            <button key={item.key} type="button" role="option" aria-selected={i === slashIndex} className={`${i === slashIndex ? 'is-on' : ''}${item.agent ? ' is-agent' : ''}`}
              onMouseEnter={() => setSlashIndex(i)} onClick={() => props.onApplySlash(item)}>
              <span>{item.label}</span><small>{item.hint}</small>
            </button>
          ))}
        </div>
      )}
      {menuOpen && (
        <div className="blk-menu" role="menu" aria-label={tr("段落菜单")}>
          <button type="button" role="menuitem" onClick={() => props.onMove(-1)} disabled={props.index === 0}>{tr("上移")}</button>
          <button type="button" role="menuitem" onClick={() => props.onMove(1)} disabled={props.index === props.count - 1}>{tr("下移")}</button>
          <button type="button" role="menuitem" onClick={props.onDuplicate}>{tr("复制段落")}</button>
          <button type="button" role="menuitem" className="is-danger" onClick={props.onRemove}>{tr("删除段落")}</button>
          {agentEnabled && block.text.trim() && <>
            <span className="blk-menu-sep" aria-hidden />
            <button type="button" role="menuitem" className="is-agent" onClick={() => props.onAsk({ where: 'block', quote: block.text, instruction: tr("改写这一段，更清晰简洁，保持原意和原有引用"), send: true })}>{tr("Agent：改写这一段")}</button>
            <button type="button" role="menuitem" className="is-agent" onClick={() => props.onAsk({ where: 'insert', quote: '', before: block.text, instruction: tr("在这一段后面续写一段，先核对课程原片段并附引用"), send: true })}>{tr("Agent：在下面续写")}</button>
            <button type="button" role="menuitem" className="is-agent" onClick={() => props.onAsk({ where: 'block', quote: block.text, instruction: tr("核对这一段与课程原片段是否一致，有出入就修正并附引用"), send: true })}>{tr("Agent：核对这段的出处")}</button>
            <button type="button" role="menuitem" onClick={() => props.onAsk({ where: 'block', quote: block.text, instruction: '', send: false })}>{tr("引用到 Agent 对话")}</button>
          </>}
        </div>
      )}
      {prompt && (
        <form className="blk-prompt" onMouseDown={(event) => { if (!(event.target instanceof HTMLInputElement)) keep(event) }}
          onSubmit={(event) => { event.preventDefault(); submitPrompt() }}>
          <div className="blk-prompt-head"><strong>Agent</strong><span className="muted small">{prompt.where === 'insert' ? tr("在这里写一段，以修改建议出现") : tr("只改动选中的内容，以修改建议出现")}</span></div>
          <input aria-label={tr("告诉 Agent 要怎么做")} autoFocus value={promptText} placeholder={prompt.where === 'insert' ? tr("例如：补充一个乘法节点的例子，并标出处") : tr("例如：补充一个例子，并标出处")}
            onChange={(event) => setPromptText(event.target.value)} onKeyDown={(event) => { if (event.key === 'Escape') { event.preventDefault(); props.onClosePrompt() } }} />
          <div className="blk-prompt-actions">
            <span className="muted small">{tr("Enter 发送 · Esc 取消")}</span>
            <button type="button" className="btn btn-small" onClick={props.onClosePrompt}>{tr("取消")}</button>
            <button type="submit" className="btn btn-small btn-primary" disabled={!promptText.trim()}>{tr("发送")}</button>
          </div>
        </form>
      )}
    </div>
  )
}
