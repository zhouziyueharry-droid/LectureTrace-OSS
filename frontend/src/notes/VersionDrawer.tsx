import { useEffect, useState } from 'react'
import { errorText, formatDateTime, getNoteVersion, listNoteVersions, type NoteVersion, type NoteVersionBody } from '../api.ts'
import CitedMarkdown from '../components/CitedMarkdown.tsx'

interface Props {
  videoId: string
  name: string
  refreshKey: string | number
  onClose: () => void
  onRestore: (versionId: string) => Promise<void>
}

export default function VersionDrawer({ videoId, name, refreshKey, onClose, onRestore }: Props) {
  const [versions, setVersions] = useState<NoteVersion[] | null>(null)
  const [selected, setSelected] = useState<string | null>(null)
  const [body, setBody] = useState<NoteVersionBody | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    let active = true
    listNoteVersions(videoId, name).then((list) => { if (active) { setVersions(list); setError(null) } }, (e) => { if (active) setError(errorText(e)) })
    return () => { active = false }
  }, [videoId, name, refreshKey])

  useEffect(() => {
    if (!selected) { setBody(null); return }
    let active = true
    getNoteVersion(videoId, name, selected).then((v) => { if (active) setBody(v) }, (e) => { if (active) setError(errorText(e)) })
    return () => { active = false }
  }, [videoId, name, selected])

  useEffect(() => {
    const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') onClose() }
    window.addEventListener('keydown', escape)
    return () => window.removeEventListener('keydown', escape)
  }, [onClose])

  const restore = async () => {
    if (!selected || busy) return
    setBusy(true)
    try { await onRestore(selected); onClose() } catch (e) { setError(errorText(e)) } finally { setBusy(false) }
  }
  const chosen = versions?.find((v) => v.id === selected)

  return (
    <aside className="nd-drawer" aria-label="版本历史">
      <div className="nd-drawer-head"><strong>版本历史</strong><span className="spacer" /><button type="button" className="btn btn-small btn-text" onClick={onClose} aria-label="关闭版本历史">关闭</button></div>
      {error && <p className="error-text small" role="alert">{error}</p>}
      {!versions && !error && <p className="muted small">读取中…</p>}
      {versions && versions.length === 0 && <p className="muted small">还没有历史版本。</p>}
      <ul className="nd-versions">
        {versions?.map((v) => (
          <li key={v.id}>
            <button type="button" className={`nd-version${selected === v.id ? ' is-selected' : ''}`} aria-pressed={selected === v.id} onClick={() => setSelected(selected === v.id ? null : v.id)}>
              <span className={`nd-who is-${v.author}`}>{v.author === 'agent' ? 'Agent' : '你'}</span>
              <span className="nd-version-text"><strong>{v.label || '编辑'}</strong><small>{formatDateTime(v.at)}{v.current ? ' · 当前' : ''}</small></span>
            </button>
          </li>
        ))}
      </ul>
      {chosen && (
        <div className="nd-preview">
          <div className="nd-preview-actions">
            <button type="button" className="btn btn-small" disabled={busy || chosen.current} onClick={() => void restore()}>{chosen.current ? '这是当前版本' : busy ? '恢复中…' : '恢复到此版本'}</button>
            <span className="muted small">恢复会生成一个新版本，不会丢掉现在的内容</span>
          </div>
          <div className="nd-preview-body">{body ? <CitedMarkdown content={body.text || '*（空页面）*'} citations={[]} onSeek={() => {}} inactive="stale" /> : <p className="muted small">读取中…</p>}</div>
        </div>
      )}
    </aside>
  )
}
