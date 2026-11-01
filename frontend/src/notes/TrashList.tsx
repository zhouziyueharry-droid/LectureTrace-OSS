import { useEffect, useState } from 'react'
import { errorText, listTrashedNotes, listTrashedPdfs, restorePdf, restoreTrashedNote, type TrashedPage } from '../api.ts'

type Item = TrashedPage & { kind: 'note' | 'pdf' }

export default function TrashList({ videoId, onRestored }: { videoId: string; onRestored: () => void }) {
  const [items, setItems] = useState<Item[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [notice, setNotice] = useState('')
  const [reload, setReload] = useState(0)
  useEffect(() => {
    let active = true
    setLoading(true)
    setError(null)
    Promise.all([listTrashedNotes(videoId), listTrashedPdfs(videoId)]).then(([notes, pdfs]) => {
      if (active) setItems([...notes.map((n): Item => ({ ...n, kind: 'note' })), ...pdfs.map((p): Item => ({ ...p, kind: 'pdf' }))])
    }, (e) => { if (active) setError(`读取回收站失败：${errorText(e)}`) })
      .finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [videoId, reload])

  const restore = async (item: Item) => {
    setBusy(item.trash_id)
    setError(null)
    setNotice('')
    try {
      if (item.kind === 'pdf') await restorePdf(videoId, item.trash_id)
      else await restoreTrashedNote(videoId, item.trash_id)
      setItems((old) => old.filter((p) => p !== item))
      setNotice(`已恢复「${item.name}」，可在文件列表中打开`)
      onRestored()
    } catch (e) { setError(`恢复失败：${errorText(e)}`) }
    finally { setBusy(null) }
  }

  return (
    <div className="pt-trash" role="group" aria-label="回收站">
      <p className="muted small">恢复后回到文件列表；不会覆盖同名笔记。</p>
      {loading && <p role="status">正在读取回收站…</p>}
      {error && <div className="error-text small" role="alert">{error}<button type="button" disabled={loading || busy !== null} onClick={() => setReload((n) => n + 1)}>重新读取</button></div>}
      {notice && <p className="small" role="status">{notice}</p>}
      {!loading && !error && items.length === 0 && <p className="muted small">回收站为空</p>}
      {items.map((item) => (
        <div className="pt-trash-row" key={`${item.kind}:${item.trash_id}`}>
          <div><span className="pt-trash-name">{item.name}</span><span className="muted small">{item.kind === 'pdf' ? 'PDF' : '笔记'}{item.deleted_at ? ` · ${new Date(item.deleted_at * 1000).toLocaleString()}` : ''}</span></div>
          <button type="button" disabled={loading || busy !== null} aria-label={`恢复 ${item.name}`} onClick={() => void restore(item)}>{busy === item.trash_id ? '恢复中…' : '恢复'}</button>
        </div>
      ))}
    </div>
  )
}
