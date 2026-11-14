import { systemText, tr, useLocale } from '../i18n.ts'
import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { formatTime, type Outline, type OutlineNode } from '../api.ts'
import type { OutlineQuote } from '../outlineQuote.ts'

interface Props {
  outline: Outline | null
  loading: boolean
  error: string | null
  placeholder: ReactNode
  currentTime: number
  onSeek: (t: number) => void
  onReference?: (quote: OutlineQuote) => void
  /** 生成大纲的设置（输出语言、模型等）已改变，大纲需重新生成 */
  needsRegen?: 'settings' | 'upstream'
  /** 重新生成大纲（会先显示发送清单）；不传则只提示 */
  onRegen?: () => void
  regenBusy?: boolean
  /** 嵌在页面区里：去掉外框和标题，保留展开/折叠操作 */
  embedded?: boolean
  headExtra?: ReactNode
}

const ADMIN_PREFIX = 'admin'

interface Flat {
  key: string
  start: number
}

interface SelectionAction extends OutlineQuote {
  x: number
  y: number
  below: boolean
}

function flatten(nodes: OutlineNode[], prefix: string, out: Flat[], parents: string[]) {
  nodes.forEach((n, i) => {
    const key = prefix ? `${prefix}.${i}` : String(i)
    if (Number.isFinite(n.start)) out.push({ key, start: n.start })
    if (n.children?.length) {
      parents.push(key)
      flatten(n.children, key, out, parents)
    }
  })
}

export default function OutlinePanel({
  outline,
  loading,
  error,
  placeholder,
  currentTime,
  onSeek,
  onReference,
  needsRegen,
  onRegen,
  regenBusy,
  embedded,
  headExtra,
}: Props) {
  useLocale()
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set())
  const [selectionAction, setSelectionAction] = useState<SelectionAction | null>(null)
  const bodyRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const dismiss = (event: PointerEvent) => {
      if (!(event.target instanceof Element) || !event.target.closest('.ol-selection-action')) setSelectionAction(null)
    }
    const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') setSelectionAction(null) }
    document.addEventListener('pointerdown', dismiss)
    document.addEventListener('keydown', escape)
    return () => {
      document.removeEventListener('pointerdown', dismiss)
      document.removeEventListener('keydown', escape)
    }
  }, [])

  useEffect(() => { setSelectionAction(null) }, [outline])

  const captureSelection = () => {
    const selected = window.getSelection()
    if (!onReference || !selected || selected.isCollapsed || !selected.rangeCount) { setSelectionAction(null); return }
    const start = selected.anchorNode?.parentElement?.closest<HTMLLIElement>('.ol-node[data-node-id]')
    const end = selected.focusNode?.parentElement?.closest<HTMLLIElement>('.ol-node[data-node-id]')
    if (!start || start !== end || !bodyRef.current?.contains(start)) { setSelectionAction(null); return }
    const excerpt = selected.toString().replace(/\s+/g, ' ').trim()
    if (!excerpt) { setSelectionAction(null); return }
    const rect = selected.getRangeAt(0).getBoundingClientRect()
    const below = rect.top < 48
    setSelectionAction({
      node_id: start.dataset.nodeId!, title: start.dataset.nodeTitle!, excerpt,
      x: Math.max(80, Math.min(window.innerWidth - 80, rect.left + rect.width / 2)),
      y: below ? rect.bottom + 8 : rect.top - 8, below,
    })
  }
  const queueSelection = () => window.requestAnimationFrame(captureSelection)

  const { flat, parents } = useMemo(() => {
    const flat: Flat[] = []
    const parents: string[] = []
    if (outline) flatten(outline.chapters, '', flat, parents)
    return { flat, parents }
  }, [outline])

  // 当前播放位置对应的节点：开始时间 ≤ 当前时间的最后一个节点
  const activeKey = useMemo(() => {
    let best: Flat | null = null
    for (const f of flat) {
      if (f.start <= currentTime + 0.25 && (!best || f.start >= best.start)) best = f
    }
    return best?.key ?? null
  }, [flat, currentTime])

  // 随播放自动滚到当前节点；用户自己滚动后暂停跟随，点「回到当前位置」或点节点跳转后恢复
  const [follow, setFollow] = useState(true)
  const autoScrollUntil = useRef(0)
  const scrollToActive = (smooth: boolean) => {
    const body = bodyRef.current
    const nodes = body?.querySelectorAll<HTMLElement>('.ol-node.is-active > .ol-head')
    const head = nodes?.[nodes.length - 1]
    if (!body || !head) return
    const top = head.getBoundingClientRect().top - body.getBoundingClientRect().top
    if (smooth && top >= 40 && top <= body.clientHeight - 80) return
    autoScrollUntil.current = Date.now() + 900
    body.scrollTo({ top: body.scrollTop + top - body.clientHeight * 0.3, behavior: smooth ? 'smooth' : 'auto' })
  }
  useEffect(() => {
    if (follow && activeKey) scrollToActive(true)
  }, [activeKey, follow])
  const onBodyScroll = () => {
    setSelectionAction(null)
    if (Date.now() > autoScrollUntil.current) setFollow(false)
  }
  const seekAndFollow = (t: number) => {
    setFollow(true)
    onSeek(t)
  }

  const toggle = (key: string) =>
    setCollapsed((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })

  const renderNodes = (nodes: OutlineNode[], prefix: string, depth: number): ReactNode => (
    <ul className={`ol-list ol-depth-${Math.min(depth, 3)}`}>
      {nodes.map((n, i) => {
        const key = prefix ? `${prefix}.${i}` : String(i)
        const hasChildren = (n.children?.length ?? 0) > 0
        const isCollapsed = collapsed.has(key)
        const isActive = activeKey === key || (isCollapsed && activeKey?.startsWith(key + '.'))
        return (
          <li key={key} className={`ol-node${isActive ? ' is-active' : ''}`} data-node-id={n.node_id} data-node-title={n.title}>
            <div className="ol-head">
              {hasChildren ? (
                <button
                  className="ol-toggle"
                  onClick={() => toggle(key)}
                  aria-expanded={!isCollapsed}
                  aria-label={isCollapsed ? tr("展开") : tr("折叠")}
                >
                  {isCollapsed ? '▸' : '▾'}
                </button>
              ) : (
                <span className="ol-toggle-spacer" />
              )}
              <NodeHead node={n} onSeek={seekAndFollow} />
            </div>
            {n.summary && <p className="ol-summary">{n.summary}</p>}
            {hasChildren && !isCollapsed && renderNodes(n.children!, key, depth + 1)}
          </li>
        )
      })}
    </ul>
  )

  const admin = outline?.admin
  const adminOpen = !collapsed.has(ADMIN_PREFIX)
  const warnings = outline?.warnings ?? []

  return (
    <section className={`panel outline-panel${embedded ? ' is-embedded' : ''}`} aria-label={tr("大纲")}>
      <div className="panel-head">
        {embedded ? <span className="muted small">{tr("由课程生成，随播放自动定位")}</span> : <h2>{tr("大纲")}</h2>}
        {outline && (
          <div className="panel-actions">
            {headExtra}
            {(parents.length > 0 || (admin?.items.length ?? 0) > 0) && (
              <>
                <button className="btn btn-small btn-text" onClick={() => setCollapsed(new Set())}>{tr("全部展开")}</button>
                <button className="btn btn-small btn-text" onClick={() => setCollapsed(new Set([...parents, ADMIN_PREFIX]))}>{tr("全部折叠")}</button>
              </>
            )}
          </div>
        )}
      </div>
      {!follow && activeKey && (
        <button type="button" className="ol-follow" onClick={() => { setFollow(true); scrollToActive(false) }}>
          <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" aria-hidden><circle cx="8" cy="8" r="2.5" /><path d="M8 1.5v2.5M8 12v2.5M1.5 8H4M12 8h2.5" /></svg>{tr("回到当前位置")}</button>
      )}
      <div className="panel-body" ref={bodyRef} onPointerUp={queueSelection} onMouseUp={queueSelection} onKeyUp={queueSelection} onScroll={onBodyScroll}>
        {error && <p className="error-text small">{tr("加载大纲失败：")}{systemText(error)}</p>}
        {outline && needsRegen && (
          <div className="alert alert-info small ol-regen">
            <span>{tr("大纲需重新生成：")}{needsRegen === 'settings'
                ? tr("生成大纲的设置（如输出语言、模型）已改变，下面仍是旧版本。")
                : tr("前面的处理结果已变化，请用上方的“重新处理”从头更新，下面仍是旧版本。")}
            </span>
            {onRegen && (
              <button className="btn btn-small" onClick={onRegen} disabled={regenBusy} title={tr("开始前会先显示发送清单与费用预估")}>{tr("重新生成")}</button>
            )}
          </div>
        )}
        {outline ? (
          <>
            {outline.chapters.length === 0 ? <p className="muted">{tr("大纲为空。")}</p> : renderNodes(outline.chapters, '', 0)}
            {admin && admin.items.length > 0 && (
              <div className="ol-admin" aria-label={admin.title}>
                <div className="ol-head ol-admin-head">
                  <button
                    className="ol-toggle"
                    onClick={() => toggle(ADMIN_PREFIX)}
                    aria-expanded={adminOpen}
                    aria-label={adminOpen ? tr("折叠") : tr("展开")}
                  >
                    {adminOpen ? '▾' : '▸'}
                  </button>
                  <span className="ol-admin-title">{admin.title}</span>
                  <span className="muted small">{admin.items.length} {tr("条")}</span>
                </div>
                {adminOpen && (
                  <ul className="ol-list ol-admin-list">
                    {admin.items.map((n, i) => (
                      <li key={i} className="ol-node" data-node-id={n.node_id} data-node-title={n.title}>
                        <div className="ol-head">
                          <NodeHead node={n} onSeek={seekAndFollow} />
                        </div>
                        {n.summary && <p className="ol-summary">{n.summary}</p>}
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            )}
            {warnings.length > 0 && (
              <details className="ol-warnings small">
                <summary>{tr("生成时有")} {warnings.length} {tr("条提示")}</summary>
                <ul>
                  {warnings.map((w, i) => (
                    <li key={i}>{systemText(w)}</li>
                  ))}
                </ul>
              </details>
            )}
          </>
        ) : loading ? (
          <p className="muted">{tr("加载中…")}</p>
        ) : (
          <div className="placeholder">{placeholder}</div>
        )}
        {selectionAction && (
          <button type="button" className={`ol-selection-action${selectionAction.below ? ' is-below' : ''}`}
            style={{ left: selectionAction.x, top: selectionAction.y }}
            onPointerDown={(event) => event.preventDefault()}
            onClick={() => {
              onReference?.(selectionAction)
              setSelectionAction(null)
              window.getSelection()?.removeAllRanges()
            }}>{tr("添加到问答")}</button>
        )}
      </div>
    </section>
  )
}

/** 时间徽标 + 标题，点击都跳转；没有有效时间（校验失败）时不可点 */
function NodeHead({ node, onSeek }: { node: OutlineNode; onSeek: (t: number) => void }) {
  useLocale()
  const ok = Number.isFinite(node.start)
  return (
    <>
      <button
        className="ts-badge"
        onClick={() => ok && onSeek(node.start)}
        disabled={!ok}
        title={ok ? tr("跳转到 {0}", formatTime(node.start)) : tr("没有可跳转的时间")}
      >
        {formatTime(node.start)}
      </button>
      <button className="ol-title" onClick={() => ok && !window.getSelection()?.toString().trim() && onSeek(node.start)}>
        {node.title}
      </button>
    </>
  )
}
