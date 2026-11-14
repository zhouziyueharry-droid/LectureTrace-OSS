import { systemText, tr, useLocale } from '../i18n.ts'
import { useEffect, useRef, useState, type FormEvent } from 'react'
import { Link } from 'react-router-dom'
import { createCourse, deletePackage, errorText, formatDateTime, formatTime, preparePackage, videoStatusLabel, type Course, type PackageProgress, type Video } from '../api.ts'
import PackageExportProgress from './PackageExportProgress.tsx'
import { DRAG_TYPE } from './AppShell.tsx'
import { useLibrary } from '../library.tsx'

const pillClass = (s: string) => `status-pill st-${s}`

/** 选课程的弹层：搜索、移出、就地新建课程 */
export function MovePopover({
  courses,
  excludeId,
  upward,
  onPick,
  onClose,
}: {
  courses: Course[]
  excludeId?: string | null
  upward?: boolean
  onPick: (courseId: string | null) => void
  onClose: () => void
}) {
  useLocale()
  const { refresh } = useLibrary()
  const [q, setQ] = useState('')
  const [creating, setCreating] = useState(false)
  const [title, setTitle] = useState('')
  const [error, setError] = useState<string | null>(null)
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose()
    }
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [onClose])

  const list = courses.filter((c) => c.title.toLowerCase().includes(q.trim().toLowerCase()))
  const create = async (e: FormEvent) => {
    e.preventDefault()
    if (!title.trim()) return
    try {
      const c = await createCourse(title)
      await refresh()
      onPick(c.id)
    } catch (err) {
      setError(errorText(err))
    }
  }

  return (
    <div className={`lt-pop${upward ? ' is-up' : ''}`} role="dialog" aria-label={tr("移动到课程")} ref={ref}>
      <input autoFocus aria-label={tr("搜索课程")} placeholder={tr("搜索课程")} value={q} onChange={(e) => setQ(e.target.value)} />
      <div className="lt-pop-list">
        {list.map((c) => (
          <button key={c.id} className="lt-pop-item" disabled={c.id === excludeId} onClick={() => onPick(c.id)}>
            <span className="ellipsis">{c.title}</span>
            {c.id === excludeId && <span className="muted small">{tr("当前")}</span>}
          </button>
        ))}
        {list.length === 0 && <span className="muted small lt-pop-empty">{tr("没有匹配的课程")}</span>}
      </div>
      <div className="lt-pop-sep" />
      {excludeId !== null && (
        <button className="lt-pop-item" onClick={() => onPick(null)}>{tr("移出课程（未归类）")}</button>
      )}
      {creating ? (
        <form className="lt-pop-new" onSubmit={(e) => void create(e)}>
          <input autoFocus aria-label={tr("新课程名称")} placeholder={tr("课程名称，回车创建并移入")} maxLength={120} value={title} onChange={(e) => setTitle(e.target.value)} />
          {error && <span className="error-text small">{systemText(error)}</span>}
        </form>
      ) : (
        <button className="lt-pop-item is-accent" onClick={() => setCreating(true)}>{tr("＋ 新建课程并移入…")}</button>
      )}
    </div>
  )
}

interface Props {
  videos: Video[]
  courses: Course[]
  /** 课程页传入当前课程；未归类页传 null；课程库搜索结果不传 */
  currentCourseId?: string | null
  showCourse?: boolean
  numbered?: boolean
  onDelete: (vs: Video[]) => void
}

export default function LectureTable({ videos, courses, currentCourseId, showCourse, numbered, onDelete }: Props) {
  useLocale()
  const { moveVideos, notify } = useLibrary()
  const [selected, setSelected] = useState<Set<string>>(new Set())
  // 默认不显示选择框；点表头的「选择」才进入批量选择
  const [selecting, setSelecting] = useState(false)
  const [menuId, setMenuId] = useState<string | null>(null)
  const [movingId, setMovingId] = useState<string | null>(null) // 'bulk' 或视频 id
  const [exportingId, setExportingId] = useState<string | null>(null)
  const [exportProgress, setExportProgress] = useState<PackageProgress | null>(null)
  const exportBusy = useRef(false)
  const ids = videos.map((v) => v.id)

  // 视频被移走/删除后，丢弃已不在表中的选中项
  useEffect(() => {
    setSelected((s) => {
      const next = new Set([...s].filter((id) => ids.includes(id)))
      return next.size === s.size ? s : next
    })
  }, [ids.join(',')]) // eslint-disable-line react-hooks/exhaustive-deps

  const allOn = videos.length > 0 && selected.size === videos.length
  const toggle = (id: string) =>
    setSelected((s) => {
      const n = new Set(s)
      if (n.has(id)) n.delete(id)
      else n.add(id)
      return n
    })
  const endSelecting = () => { setSelecting(false); setSelected(new Set()) }
  const courseName = (id: string | null) => courses.find((c) => c.id === id)?.title

  const move = async (idList: string[], courseId: string | null) => {
    setMovingId(null)
    setMenuId(null)
    await moveVideos(idList, courseId)
    setSelected(new Set())
  }

  const onDragStart = (e: React.DragEvent, v: Video) => {
    const dragIds = selected.has(v.id) ? [...selected] : [v.id]
    e.dataTransfer.setData(DRAG_TYPE, JSON.stringify(dragIds))
    e.dataTransfer.effectAllowed = 'move'
  }

  const exportVideo = async (video: Video) => {
    if (exportBusy.current || video.status !== 'done') return
    exportBusy.current = true
    setExportingId(video.id)
    setExportProgress(null)
    setMenuId(null)
    notify(tr("正在准备课程包（含原视频），大文件可能需要几分钟"))
    let token: string | null = null
    try {
      const prepared = await preparePackage(video.id, setExportProgress)
      token = prepared.token
      const link = document.createElement('a')
      link.href = prepared.download_url
      link.download = prepared.filename
      document.body.appendChild(link)
      link.click()
      link.remove()
      notify(tr("课程包已交给浏览器下载，可在下载列表查看进度"))
    } catch (e) {
      if (token) await deletePackage(token).catch(() => {})
      notify(tr("课程包导出失败：{0}", errorText(e)), 'error')
    } finally {
      exportBusy.current = false
      setExportingId(null)
    }
  }

  return (
    <div className={`lt-table${showCourse ? ' has-course' : ''}${numbered ? ' has-num' : ''}${selecting ? '' : ' no-select'}`}>
      <div className="lt-tr lt-th" role="row">
        {selecting && (
          <span>
            <input type="checkbox" aria-label={tr("全选")} checked={allOn} ref={(el) => { if (el) el.indeterminate = selected.size > 0 && !allOn }}
              onChange={() => setSelected(allOn ? new Set() : new Set(ids))} />
          </span>
        )}
        {numbered && <span>{tr("讲次")}</span>}
        <span>{tr("标题")}</span>
        {showCourse && <span>{tr("课程")}</span>}
        <span>{tr("时长")}</span>
        <span>{tr("状态")}</span>
        <span>{tr("导入时间")}</span>
        <span>
          <button type="button" className="lt-select-btn" aria-pressed={selecting} onClick={() => (selecting ? endSelecting() : setSelecting(true))}
            title={selecting ? tr("退出批量选择") : tr("批量选择讲次，一次移动或删除多个")}>
            {selecting ? tr("完成") : tr("选择")}
          </button>
        </span>
      </div>
      {videos.map((v, i) => (
        <div key={v.id} className={`lt-tr${selected.has(v.id) ? ' is-selected' : ''}`} draggable onDragStart={(e) => onDragStart(e, v)}>
          {selecting && (
            <span>
              <input type="checkbox" aria-label={tr("选择 {0}", v.title || v.filename)} checked={selected.has(v.id)} onChange={() => toggle(v.id)} />
            </span>
          )}
          {numbered && <span className="mono muted small">{String(i + 1).padStart(2, '0')}</span>}
          <div className="lt-cell-title">
            <Link className="vlist-title" to={`/videos/${encodeURIComponent(v.id)}`}>{v.title || v.filename}</Link>
            <span className="muted small ellipsis">{v.filename}</span>
            {exportingId === v.id && <PackageExportProgress progress={exportProgress} />}
          </div>
          {showCourse && (
            <span className="small">
              {v.course_id ? <Link to={`/courses/${encodeURIComponent(v.course_id)}`}>{courseName(v.course_id) ?? tr("课程")}</Link> : <span className="muted">{tr("未归类")}</span>}
            </span>
          )}
          <span className="mono small">{v.duration_s != null ? formatTime(v.duration_s) : '—'}</span>
          <span><span className={pillClass(v.status)}>{videoStatusLabel(v.status)}</span></span>
          <span className="muted small">{formatDateTime(v.created_at)}</span>
          <span className="lt-row-menu">
            <button className="icon-btn" aria-label={tr("{0} 的更多操作", v.title || v.filename)} aria-haspopup="menu" aria-expanded={menuId === v.id}
              onClick={() => setMenuId(menuId === v.id ? null : v.id)}>⋯</button>
            {menuId === v.id && movingId !== v.id && (
              <div className="lt-menu" role="menu">
                <Link role="menuitem" to={`/videos/${encodeURIComponent(v.id)}`}>{tr("打开")}</Link>
                <button role="menuitem" onClick={() => setMovingId(v.id)}>{tr("移动到课程…")}</button>
                {v.status === 'done' && <button role="menuitem" disabled={exportingId !== null} onClick={() => void exportVideo(v)}>{tr("导出课程包")}</button>}
                {v.course_id && <button role="menuitem" onClick={() => void move([v.id], null)}>{tr("移出课程")}</button>}
                <button role="menuitem" className="is-danger" onClick={() => { setMenuId(null); onDelete([v]) }}>{tr("删除视频…")}</button>
              </div>
            )}
            {movingId === v.id && (
              <MovePopover courses={courses} excludeId={v.course_id} onPick={(cid) => void move([v.id], cid)} onClose={() => { setMovingId(null); setMenuId(null) }} />
            )}
          </span>
        </div>
      ))}

      {selected.size > 0 && (
        <div className="lt-bulk" role="toolbar" aria-label={tr("批量操作")}>
          <span className="lt-bulk-count">{tr("已选")} {selected.size} {tr("讲")}</span>
          <div className="lt-bulk-move">
            <button onClick={() => setMovingId(movingId === 'bulk' ? null : 'bulk')}>{tr("移动到课程")}</button>
            {movingId === 'bulk' && (
              <MovePopover upward courses={courses} excludeId={currentCourseId} onPick={(cid) => void move([...selected], cid)} onClose={() => setMovingId(null)} />
            )}
          </div>
          {currentCourseId && <button onClick={() => void move([...selected], null)}>{tr("从课程移出")}</button>}
          <button className="is-danger" onClick={() => onDelete(videos.filter((v) => selected.has(v.id)))}>{tr("删除")}</button>
          <button aria-label={tr("取消选择")} onClick={endSelecting}>×</button>
        </div>
      )}
    </div>
  )
}
