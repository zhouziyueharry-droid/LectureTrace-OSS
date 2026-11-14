import { systemText, tr, useLocale } from '../i18n.ts'
import { useEffect, useRef, useState } from 'react'
import type { NoteItem } from '../api.ts'
import TrashList from './TrashList.tsx'

export type RowAction = 'rename' | 'duplicate' | 'export' | 'delete'
export const OUTLINE_PAGE = 'outline'

export const pageTitle = (name: string) => name.replace(/\.md$/i, '')

export interface PageHandlers {
  page: string
  onOpen: (page: string) => void
  onNew: () => void
  onImport: () => void
  importing: boolean
  onAction: (name: string, action: RowAction) => void
}

function Lock() {
  useLocale()
  return (
    <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <rect x="3.5" y="7" width="9" height="6.5" rx="1.5" /><path d="M5.5 7V5a2.5 2.5 0 015 0v2" />
    </svg>
  )
}

function Doc() {
  useLocale()
  return (
    <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M4 2.5h5l3 3V13a.5.5 0 01-.5.5h-7.5a.5.5 0 01-.5-.5V3a.5.5 0 01.5-.5zM9 2.5V5.5h3" />
    </svg>
  )
}

function Badges({ item }: { item: NoteItem }) {
  useLocale()
  return (
    <>
      {item.pdf && <span className="pg-badge">PDF</span>}
      {item.agent_new && <span className="pg-badge" title={tr("Agent 新建，你还没打开过")}>{tr("Agent 新建")}</span>}
      {item.has_proposal && <span className="pg-badge is-review" title={tr("有待审阅的 Agent 建议")}>{tr("待审阅")}</span>}
    </>
  )
}

export function RowMenu({ name, onAction, label }: { name: string; onAction: (name: string, action: RowAction) => void; label: string }) {
  useLocale()
  const [open, setOpen] = useState(false)
  const root = useRef<HTMLSpanElement>(null)
  useEffect(() => {
    if (!open) return
    const dismiss = (event: MouseEvent) => { if (!root.current?.contains(event.target as Node)) setOpen(false) }
    const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') setOpen(false) }
    document.addEventListener('click', dismiss)
    document.addEventListener('keydown', escape)
    return () => { document.removeEventListener('click', dismiss); document.removeEventListener('keydown', escape) }
  }, [open])
  const run = (action: RowAction) => { setOpen(false); onAction(name, action) }
  return (
    <span className="pg-menu-wrap" ref={root}>
      <button type="button" className="pg-more" aria-label={tr("{0}的菜单", label)} aria-expanded={open} onClick={(event) => { event.stopPropagation(); setOpen((v) => !v) }}>⋯</button>
      {open && (
        <div className="nd-menu pg-menu" role="menu">
          {!name.startsWith('pdf:') && <button type="button" role="menuitem" onClick={() => run('rename')}>{tr("重命名")}</button>}
          {!name.startsWith('pdf:') && <button type="button" role="menuitem" onClick={() => run('duplicate')}>{tr("创建副本")}</button>}
          <button type="button" role="menuitem" onClick={() => run('export')}>{name.startsWith('pdf:') ? tr("下载 PDF") : tr("导出 Markdown")}</button>
          <button type="button" role="menuitem" className="is-danger" onClick={() => run('delete')}>{tr("移到回收站")}</button>
        </div>
      )}
    </span>
  )
}

interface TreeProps extends PageHandlers {
  videoId: string
  hidden: boolean
  items: NoteItem[]
  loading: boolean
  error: string | null
  onRetry: () => void
}

/** 文件栏独立于文档挂载，收起不影响编辑状态。 */
export function PageTree({ videoId, hidden, items, loading, error, onRetry, page, onOpen, onNew, onImport, importing, onAction }: TreeProps) {
  useLocale()
  const [query, setQuery] = useState('')
  const [trash, setTrash] = useState(false)
  const shown = items.filter((i) => (i.pdf?.name ?? pageTitle(i.name)).toLowerCase().includes(query.trim().toLowerCase()))
  return (
    <aside id="page-files" className="pg-tree" aria-label={tr("文件栏")} hidden={hidden}>
      <div className="pg-create-actions">
        <button type="button" className="btn btn-small" onClick={onNew}>{tr("新建笔记")}</button>
        <button type="button" className="btn btn-small" disabled={importing} onClick={onImport}>{importing ? tr("正在导入…") : tr("导入 PDF")}</button>
      </div>
      {trash ? <div className="pg-list">
        <button type="button" className="pg-row" onClick={() => setTrash(false)}>{tr("← 返回文件列表")}</button>
        <TrashList videoId={videoId} onRestored={onRetry} />
      </div> : <>
      <div className="pg-search"><input type="search" aria-label={tr("搜索文件")} placeholder={tr("搜索文件")} value={query} onChange={(event) => setQuery(event.target.value)} /></div>
      <div className="pg-list">
        <button type="button" className={`pg-row${page === OUTLINE_PAGE ? ' is-on' : ''}`} aria-current={page === OUTLINE_PAGE ? 'page' : undefined} onClick={() => onOpen(OUTLINE_PAGE)}>
          <Lock /><span className="ellipsis">{tr("大纲")}</span><small className="muted">{tr("自动生成")}</small>
        </button>
        <div className="pg-label"><span>{tr("笔记与附件")}</span><span className="muted">{items.length}</span></div>
        {shown.map((item) => (
          <div key={item.name} className={`pg-row-wrap${page === item.name ? ' is-on' : ''}`}>
            <button type="button" className={`pg-row${page === item.name ? ' is-on' : ''}`} aria-current={page === item.name ? 'page' : undefined} title={item.pdf?.name ?? pageTitle(item.name)} onClick={() => onOpen(item.name)}>
              <Doc /><span className="ellipsis">{item.pdf?.name ?? pageTitle(item.name)}</span><Badges item={item} />
            </button>
            <RowMenu name={item.name} label={item.pdf?.name ?? pageTitle(item.name)} onAction={onAction} />
          </div>
        ))}
        {loading && <p className="muted small" role="status">{tr("读取页面…")}</p>}
        {!loading && !error && shown.length === 0 && <p className="muted small pg-empty">{items.length ? tr("没有匹配的文件") : tr("还没有笔记或附件")}</p>}
        {error && <div className="error-text small pg-empty" role="alert">{tr("文件列表读取失败：")}{systemText(error)}<button type="button" className="btn btn-small btn-text" disabled={loading} onClick={onRetry}>{tr("重试")}</button></div>}
      </div>
      <div className="pg-footer"><button type="button" className="pg-row" onClick={() => setTrash(true)}>{tr("回收站")}</button></div>
      </>}
    </aside>
  )
}
