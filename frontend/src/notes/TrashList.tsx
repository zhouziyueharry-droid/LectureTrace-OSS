import { systemText, getLocale, tr, useLocale } from '../i18n.ts'
import { useEffect, useState } from 'react'
import { errorText, listTrashedNotes, listTrashedPdfs, restorePdf, restoreTrashedNote, type TrashedPage } from '../api.ts'

type Item = TrashedPage & { kind: 'note' | 'pdf' }

export default function TrashList({ videoId, onRestored }: { videoId: string; onRestored: () => void }) {
  useLocale()
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
    }, (e) => { if (active) setError(tr("读取回收站失败：{0}", errorText(e))) })
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
      setNotice(tr("已恢复「{0}」，可在文件列表中打开", item.name))
      onRestored()
    } catch (e) { setError(tr("恢复失败：{0}", errorText(e))) }
    finally { setBusy(null) }
  }

  return (
    <div className="pt-trash" role="group" aria-label={tr("回收站")}>
      <p className="muted small">{tr("恢复后回到文件列表；不会覆盖同名笔记。")}</p>
      {loading && <p role="status">{tr("正在读取回收站…")}</p>}
      {error && <div className="error-text small" role="alert">{systemText(error)}<button type="button" disabled={loading || busy !== null} onClick={() => setReload((n) => n + 1)}>{tr("重新读取")}</button></div>}
      {notice && <p className="small" role="status">{systemText(notice)}</p>}
      {!loading && !error && items.length === 0 && <p className="muted small">{tr("回收站为空")}</p>}
      {items.map((item) => (
        <div className="pt-trash-row" key={`${item.kind}:${item.trash_id}`}>
          <div><span className="pt-trash-name">{item.name}</span><span className="muted small">{item.kind === 'pdf' ? 'PDF' : tr("笔记")}{item.deleted_at ? ` · ${new Date(item.deleted_at * 1000).toLocaleString(getLocale())}` : ''}</span></div>
          <button type="button" disabled={loading || busy !== null} aria-label={tr("恢复 {0}", item.name)} onClick={() => void restore(item)}>{busy === item.trash_id ? tr("恢复中…") : tr("恢复")}</button>
        </div>
      ))}
    </div>
  )
}
