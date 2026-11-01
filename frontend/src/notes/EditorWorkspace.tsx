import { useEffect, useRef, useState, type ReactNode } from 'react'
import type { NoteItem } from '../api.ts'
import Splitter from '../components/Splitter.tsx'
import { clamp } from '../useLayoutPrefs.ts'
import { pageTitle } from './PageList.tsx'
import type { EditorLayout } from './editorLayout.ts'

interface Props {
  layout: EditorLayout
  items: NoteItem[]
  onOpen: (name: string) => void
  onClose: (name: string) => void
  onFocus: (group: number) => void
  onSplit: () => void
  onMerge: () => void
  onBrowse: () => void
  render: (name: string) => ReactNode
}

export default function EditorWorkspace({ layout, items, onOpen, onClose, onFocus, onSplit, onMerge, onBrowse, render }: Props) {
  const root = useRef<HTMLDivElement>(null)
  const [weights, setWeights] = useState<Record<number, number>>({})
  const { groups, focused, opened } = layout
  const active = groups.find(g => g.id === focused)?.active
  useEffect(() => {
    // A reference can hide its source pane on narrow screens. Keep keyboard focus visible.
    const element = document.activeElement as HTMLElement | null
    if (!element || element === document.body || !element.getClientRects().length) {
      root.current?.querySelector<HTMLButtonElement>(`[data-header="${focused}"] [aria-selected="true"]`)?.focus({ preventScroll: true })
    }
  }, [focused, active])
  const title = (name: string) => name === 'outline' ? '大纲' : items.find(i => i.name === name)?.pdf?.name ?? pageTitle(name)
  const width = (id: number) => root.current?.querySelector<HTMLElement>(`[data-header="${id}"]`)?.clientWidth ?? 400
  return <div className="editor-workspace">
    {groups.length > 1 && <nav className="editor-group-switch" aria-label="编辑组">
      {groups.map((g, i) => <button key={g.id} className={g.id === focused ? 'is-on' : ''} aria-pressed={g.id === focused} onClick={() => onFocus(g.id)}>组 {i + 1} · {g.active ? title(g.active) : '空白'}</button>)}
      <button onClick={onMerge}>合并分栏</button>
    </nav>}
    <div id="editor-documents" ref={root} className="editor-canvas" style={{ gridTemplateColumns: groups.map(g => `minmax(240px, ${weights[g.id] ?? 1}fr)`).join(' 5px ') }}>
      {groups.map((group, i) => <div key={group.id} data-header={group.id} data-current={group.id === focused} className={`editor-group-head${group.id === focused ? ' is-active' : ''}`} style={{ gridColumn: i * 2 + 1, gridRow: 1 }} onPointerDown={() => onFocus(group.id)} onFocusCapture={() => onFocus(group.id)}>
        <div className="editor-tabs" role="tablist" aria-label={`编辑组 ${i + 1}`} onKeyDown={event => {
          if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return
          const buttons = [...event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="tab"]')]
          const index = buttons.indexOf(document.activeElement as HTMLButtonElement)
          if (index < 0 || !buttons.length) return
          event.preventDefault()
          const next = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1 : (index + (event.key === 'ArrowRight' ? 1 : -1) + buttons.length) % buttons.length
          buttons[next].click(); buttons[next].focus()
        }}>
          {group.tabs.map(name => <span key={name} className={`editor-tab${group.active === name ? ' is-active' : ''}`}>
            <button role="tab" aria-selected={group.active === name} tabIndex={group.active === name ? 0 : -1} title={title(name)} onClick={() => onOpen(name)}>
              {name !== 'outline' && <span className="editor-file-kind" aria-hidden>{name.startsWith('pdf:') ? 'PDF' : 'MD'}</span>}<span className="ellipsis">{title(name)}</span>
              {items.find(item => item.name === name)?.has_proposal && <span className="pg-badge is-review">待审阅</span>}
            </button>
            <button className="editor-tab-close" aria-label={`关闭页签 ${title(name)}`} onClick={() => onClose(name)}><svg width="12" height="12" viewBox="0 0 16 16" stroke="currentColor" strokeWidth="1.5" aria-hidden><path d="m4 4 8 8m0-8-8 8" /></svg></button>
          </span>)}
        </div>
        <button className="editor-action" disabled={!group.active} aria-label="向右分栏" title={group.tabs.length > 1 ? '将当前文件移到右侧新组' : '新建右侧编辑组，打开其他文件'} onClick={onSplit}><svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" aria-hidden><rect x="2" y="3" width="12" height="10" rx="1"/><path d="M8 3v10"/></svg></button>
        {groups.length > 1 && <button className="editor-action" aria-label="合并所有分栏" title="合并所有分栏" onClick={onMerge}><svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" aria-hidden><rect x="2" y="3" width="12" height="10" rx="1"/><path d="m10 6-2 2 2 2M5 6v4"/></svg></button>}
      </div>)}
      {groups.slice(0, -1).map((group, i) => <div className="editor-divider" key={group.id} style={{ gridColumn: i * 2 + 2, gridRow: '1 / 3' }}>
        <Splitter direction="col" label={`调整编辑组 ${i + 1} 宽度`} controls="editor-documents" value={width(group.id)} min={240} max={Math.max(240, width(group.id) + width(groups[i + 1].id) - 240)}
          onStart={() => width(group.id)} onResize={size => {
            const next = groups[i + 1].id
            const total = width(group.id) + width(next)
            const w = clamp(size, 240, Math.max(240, total - 240))
            const sum = (weights[group.id] ?? 1) + (weights[next] ?? 1)
            setWeights(old => ({ ...old, [group.id]: sum * w / total, [next]: sum * (total - w) / total }))
          }} onReset={() => setWeights({})} />
      </div>)}
      {/* Stable document order/parent: moving an EmbedPDF DOM node destroys its editor. */}
      {opened.map(name => {
        const i = groups.findIndex(g => g.tabs.includes(name))
        const group = groups[i]
        return <div key={name} className="editor-document" data-current={group?.id === focused} hidden={!group || group.active !== name} style={{ gridColumn: i * 2 + 1, gridRow: 2 }} role="tabpanel" aria-label={title(name)} onFocusCapture={() => onFocus(group.id)} onPointerDownCapture={() => onFocus(group.id)}>
          {render(name)}
        </div>
      })}
      {groups.filter(g => !g.active).map(group => <div key={`empty-${group.id}`} className="editor-empty" data-current={group.id === focused} style={{ gridColumn: groups.indexOf(group) * 2 + 1, gridRow: 2 }}>
        <p>打开笔记或 PDF</p><button className="btn btn-small" onClick={onBrowse}>浏览课程文件</button>
      </div>)}
    </div>
  </div>
}
