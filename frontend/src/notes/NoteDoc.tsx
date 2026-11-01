import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  discardNoteProposal, errorText, getNoteProposal, renameNote, resolveNoteProposal, restoreNoteVersion,
  type NoteProposal, type NoteTarget,
} from '../api.ts'
import { useNoteDocument } from '../components/useNoteDocument.ts'
import BlockEditor, { type AgentAsk } from './BlockEditor.tsx'
import { joinBlocks, parseBlocks } from './blocks.ts'
import SuggestionCard, { type Decision } from './SuggestionCard.tsx'
import { applyHunks, diffBlocks, locate } from './suggestions.ts'
import VersionDrawer from './VersionDrawer.tsx'

interface Props {
  videoId: string
  name: string
  refreshKey: number
  onChanged: () => void
  onSeek: (t: number) => void
  getTime: () => number
  agentEnabled: boolean
  onAgent: (ask: AgentAsk) => void
  autoFocusTitle?: boolean
  onRenamed: (oldName: string, newName: string) => void
  onDelete: (name: string) => void
  onDuplicate: (name: string) => void
  onExport: (name: string, text: string) => void
  registerFlush: (name: string, fn: (() => Promise<NoteTarget | null>) | null) => void
}

const ILLEGAL = /[<>:"/\\|?*\x00-\x1f]/g

const STATUS: Record<string, string> = {
  loading: '读取中…', saved: '已自动保存', dirty: '正在输入…', saving: '保存中…', error: '保存失败', conflict: '有新版本待处理',
}

export default function NoteDoc(props: Props) {
  const { videoId, name, refreshKey, onChanged, onSeek, getTime, agentEnabled, onAgent, autoFocusTitle, onRenamed, onDelete, onDuplicate, onExport, registerFlush } = props
  const doc = useNoteDocument(videoId, name, refreshKey, onChanged)
  const base = name.replace(/\.md$/i, '')
  const [title, setTitle] = useState(base)
  const [editingTitle, setEditingTitle] = useState(false)
  const [proposal, setProposal] = useState<NoteProposal | null>(null)
  const [decisions, setDecisions] = useState<Record<string, Decision>>({})
  const [drawer, setDrawer] = useState(false)
  const [menu, setMenu] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  const [reviewBusy, setReviewBusy] = useState(false)
  const finishing = useRef(false)
  const titleRef = useRef<HTMLInputElement>(null)
  const flushRef = useRef(doc.flush)
  flushRef.current = doc.flush

  useEffect(() => setTitle(base), [base])
  useEffect(() => {
    if (autoFocusTitle) setEditingTitle(true)
  }, [autoFocusTitle])
  useEffect(() => { if (editingTitle) { titleRef.current?.focus(); titleRef.current?.select() } }, [editingTitle])

  useEffect(() => {
    registerFlush(name, async () => {
      const note = await flushRef.current()
      return note ? { name, revision: note.revision } : null
    })
    return () => registerFlush(name, null)
  }, [name, registerFlush])

  useEffect(() => {
    if (!menu) return
    const dismiss = (event: PointerEvent) => { if (!(event.target instanceof Element) || !event.target.closest('.nd-menu, .nd-more')) setMenu(false) }
    const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') setMenu(false) }
    document.addEventListener('pointerdown', dismiss)
    document.addEventListener('keydown', escape)
    return () => { document.removeEventListener('pointerdown', dismiss); document.removeEventListener('keydown', escape) }
  }, [menu])

  // ----- Agent 建议 -----
  const loadProposal = useCallback(async () => {
    try { setProposal(await getNoteProposal(videoId, name)) } catch { /* 读取失败时按没有建议处理，页面照常可编辑 */ }
  }, [videoId, name])
  useEffect(() => { void loadProposal() }, [loadProposal, refreshKey])
  useEffect(() => {
    window.addEventListener('focus', loadProposal)
    return () => window.removeEventListener('focus', loadProposal)
  }, [loadProposal])
  useEffect(() => { setDecisions({}); finishing.current = false }, [proposal?.id])

  const hunks = useMemo(() => (proposal ? diffBlocks(parseBlocks(proposal.base_text), parseBlocks(proposal.text)) : []), [proposal])
  const current = useMemo(() => parseBlocks(doc.text), [doc.text])
  const { placed, conflicts } = useMemo(() => {
    const placed = hunks.filter((h) => locate(current, h))
    return { placed, conflicts: hunks.filter((h) => !locate(current, h)) }
  }, [hunks, current])
  const pendingPlaced = placed.filter((h) => !decisions[h.id])
  const pendingConflicts = conflicts.filter((h) => !decisions[h.id])
  const decide = (id: string, decision: Decision | undefined) => setDecisions((old) => {
    const next = { ...old }
    if (decision) next[id] = decision
    else delete next[id]
    return next
  })

  const finish = useCallback(async () => {
    if (!proposal || finishing.current) return
    finishing.current = true
    setReviewBusy(true)
    setNotice(null)
    try {
      const note = await flushRef.current()
      if (!note) throw new Error('页面还有没处理的冲突，请先处理再完成审阅')
      const accepted = new Set(hunks.filter((h) => decisions[h.id] === 'accept').map((h) => h.id))
      const applied = applyHunks(parseBlocks(note.text), hunks, accepted)
      await resolveNoteProposal(videoId, name, {
        proposal_id: proposal.id, text: joinBlocks(applied.blocks), base_revision: note.revision,
        accepted: Math.max(0, accepted.size - applied.conflicts.length), total: hunks.length,
      })
      setProposal(null)
      await doc.refresh()
      onChanged()
    } catch (e) {
      setNotice(errorText(e))
      finishing.current = false
    } finally { setReviewBusy(false) }
  }, [proposal, hunks, decisions, videoId, name, doc, onChanged])

  useEffect(() => {
    if (proposal && hunks.length > 0 && pendingPlaced.length === 0 && pendingConflicts.length === 0) void finish()
  }, [proposal, hunks.length, pendingPlaced.length, pendingConflicts.length, finish])

  const decideAll = (decision: Decision) => setDecisions((old) => {
    const next = { ...old }
    for (const h of pendingPlaced) next[h.id] = decision
    if (decision === 'reject') for (const h of pendingConflicts) next[h.id] = 'reject'
    return next
  })
  const dropProposal = async () => {
    try { await discardNoteProposal(videoId, name); setProposal(null); setNotice(null); onChanged() } catch (e) { setNotice(errorText(e)) }
  }

  // ----- 标题即文件名 -----
  const commitTitle = async () => {
    const next = title.replace(ILLEGAL, ' ').replace(/\s+/g, ' ').trim()
    if (!next || next === base) { setTitle(base); return }
    setNotice(null)
    const note = await doc.flush()
    if (!note) { setTitle(base); return }
    try {
      const renamed = await renameNote(videoId, name, `${next}.md`, note.revision)
      onRenamed(name, renamed.name)
    } catch (e) { setNotice(errorText(e)); setTitle(base) }
  }

  const restore = async (versionId: string) => {
    const note = await doc.flush()
    if (!note) throw new Error('页面有没处理的冲突，请先处理再恢复')
    await restoreNoteVersion(videoId, name, versionId, note.revision)
    await doc.refresh()
    onChanged()
  }

  const stale = doc.base?.stale ?? false
  const citations = doc.base?.citations ?? []
  const reviewOpen = proposal !== null && hunks.length > 0 && (pendingPlaced.length > 0 || pendingConflicts.length > 0)
  const statusText = doc.storageError ? '本机草稿暂存失败，请等保存完成再切换' : STATUS[doc.status]

  return (
    <section className="note-doc" aria-label={`页面 ${base}`}>
      <header className="nd-head">
        <span className="nd-crumb">笔记</span>
        <span className="spacer" />
        <span className={`nd-status is-${doc.status}`} role="status">{statusText}</span>
        {doc.status === 'error' && <button type="button" className="btn btn-small" onClick={doc.retry}>重试</button>}
        <button type="button" className="btn btn-small btn-text" onClick={() => setDrawer((v) => !v)} aria-pressed={drawer}>历史</button>
        <div className="nd-more-wrap">
          <button type="button" className="btn btn-small btn-text nd-more" aria-label="更多操作" aria-expanded={menu} onClick={() => setMenu((v) => !v)}>⋯</button>
          {menu && (
            <div className="nd-menu" role="menu">
              <button type="button" role="menuitem" onClick={() => { setMenu(false); setEditingTitle(true) }}>重命名</button>
              <button type="button" role="menuitem" onClick={() => { setMenu(false); void doc.flush().then((note) => onExport(name, note?.text ?? doc.text)) }}>导出 Markdown</button>
              <button type="button" role="menuitem" onClick={() => { setMenu(false); onDuplicate(name) }}>创建副本</button>
              <button type="button" role="menuitem" className="is-danger" onClick={() => { setMenu(false); onDelete(name) }}>移到回收站</button>
            </div>
          )}
        </div>
      </header>

      {reviewOpen && (
        <div className="nd-review" role="region" aria-label="审阅 Agent 的修改">
          <span className="sg-chip">Agent</span>
          <strong title="原文还没有变，接受后才写入">提出了 {pendingPlaced.length + pendingConflicts.length} 处修改</strong>
          <span className="spacer" />
          <button type="button" className="btn btn-small" disabled={reviewBusy} onClick={() => decideAll('reject')}>全部拒绝</button>
          {pendingPlaced.length > 0
            ? <button type="button" className="btn btn-small sg-accept" disabled={reviewBusy} onClick={() => decideAll('accept')}>全部接受</button>
            : <button type="button" className="btn btn-small sg-accept" disabled={reviewBusy} onClick={() => decideAll('reject')}>完成审阅</button>}
        </div>
      )}
      {notice && (
        <div className="alert alert-warn small nd-alert" role="alert"><span>{notice}</span>
          {proposal && <button type="button" className="btn btn-small" onClick={() => void dropProposal()}>放弃这份建议</button>}
          <button type="button" className="btn btn-small btn-text" onClick={() => setNotice(null)}>知道了</button></div>
      )}
      {doc.status === 'error' && doc.error && !doc.loaded && (
        <div className="alert alert-warn small nd-alert" role="alert"><span>{doc.error}</span><button type="button" className="btn btn-small" onClick={() => void doc.refresh()}>重试读取</button></div>
      )}
      {doc.status === 'conflict' && (
        <div className="alert alert-warn small nd-alert" role="alert">
          <span>这个页面在别处被改过，并且改到了你正在改的同一段。</span>
          <button type="button" className="btn btn-small" onClick={() => doc.resolveConflict('mine')}>用我的版本覆盖</button>
          <button type="button" className="btn btn-small" onClick={() => doc.resolveConflict('theirs')}>载入最新版本</button>
        </div>
      )}

      <div className="nd-scroll">
        <article className="nd-article">
          {editingTitle && <input ref={titleRef} className="nd-title" aria-label="页面标题（也是文件名）" value={title} maxLength={60} placeholder="无标题" spellCheck={false}
            onChange={(event) => setTitle(event.target.value)} onBlur={() => { void commitTitle(); setEditingTitle(false) }}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && !event.nativeEvent.isComposing) { event.preventDefault(); event.currentTarget.blur() }
              if (event.key === 'Escape') { setTitle(base); event.currentTarget.blur() }
            }} />}
          {doc.loaded ? (
            <BlockEditor text={doc.text} onChange={doc.edit} citations={citations} stale={stale} onSeek={onSeek} getTime={getTime}
              onAgent={agentEnabled ? onAgent : undefined} onSave={() => void doc.flush()} readOnly={doc.status === 'conflict'}
              suggestions={placed} renderSuggestion={(hunk) => (
                <SuggestionCard hunk={hunk} decision={decisions[hunk.id]} citations={citations} stale={stale} onSeek={onSeek}
                  onDecide={(decision) => decide(hunk.id, decision)} />
              )} />
          ) : <p className="muted nd-loading">{doc.error ? '无法打开页面' : '正在打开页面…'}</p>}
          {conflicts.length > 0 && (
            <section className="nd-conflicts" aria-label="无法自动应用的建议">
              <h3>无法自动应用的建议</h3>
              <p className="muted small">这些建议改的是你后来改过的段落。可以参考后手动修改，用完点「忽略」。</p>
              {conflicts.map((hunk) => (
                <SuggestionCard key={hunk.id} hunk={hunk} conflict decision={decisions[hunk.id]} citations={citations} stale={stale} onSeek={onSeek}
                  onDecide={(decision) => decide(hunk.id, decision)} />
              ))}
            </section>
          )}
        </article>
      </div>
      {drawer && <VersionDrawer videoId={videoId} name={name} refreshKey={`${refreshKey}:${doc.base?.revision ?? ''}`} onClose={() => setDrawer(false)} onRestore={restore} />}
    </section>
  )
}
