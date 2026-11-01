import { useEffect, useRef, useState, type DragEvent, type FormEvent } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { deleteCourse, deletePackage, errorText, formatTime, prepareCoursePackage, renameCourse, setVideoCourse, type PackageProgress, type Video } from '../api.ts'
import PackageExportProgress from '../components/PackageExportProgress.tsx'
import DeleteVideosDialog from '../components/DeleteVideosDialog.tsx'
import LectureTable from '../components/LectureTable.tsx'
import { ConfirmDialog } from '../components/Modal.tsx'
import { useLibrary } from '../library.tsx'

export default function CoursePage() {
  const { id = '' } = useParams()
  const navigate = useNavigate()
  const { courses, videos, error, refresh, openImport, notify } = useLibrary()
  const [editing, setEditing] = useState(false)
  const [name, setName] = useState('')
  const [nameError, setNameError] = useState<string | null>(null)
  const [menuOpen, setMenuOpen] = useState(false)
  const [deleteOpen, setDeleteOpen] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [deleteError, setDeleteError] = useState<string | null>(null)
  const [toDelete, setToDelete] = useState<Video[] | null>(null)
  const [dragOver, setDragOver] = useState(false)
  const [exporting, setExporting] = useState(false)
  const [exportProgress, setExportProgress] = useState<PackageProgress | null>(null)
  const [exportError, setExportError] = useState<string | null>(null)
  const exportBusy = useRef(false)
  const menuRef = useRef<HTMLDivElement>(null)

  const course = courses?.find((c) => c.id === id)
  useEffect(() => {
    if (course) document.title = `${course.title} - LectureTrace`
  }, [course?.title]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    setEditing(false)
    setMenuOpen(false)
  }, [id])
  useEffect(() => {
    if (!menuOpen) return
    const onDown = (e: MouseEvent) => !menuRef.current?.contains(e.target as Node) && setMenuOpen(false)
    document.addEventListener('mousedown', onDown)
    return () => document.removeEventListener('mousedown', onDown)
  }, [menuOpen])

  const lectures = (videos?.filter((v) => v.course_id === id) ?? []).sort((a, b) => a.created_at.localeCompare(b.created_at))
  const inboxCount = videos?.filter((v) => !v.course_id).length ?? 0

  const saveName = async (e: FormEvent) => {
    e.preventDefault()
    if (!course) return
    if (!name.trim() || name.trim() === course.title) {
      setEditing(false)
      return
    }
    try {
      await renameCourse(id, name)
      await refresh()
      setEditing(false)
      setNameError(null)
    } catch (err) {
      setNameError(errorText(err))
    }
  }

  const removeCourse = async () => {
    setDeleting(true)
    setDeleteError(null)
    try {
      // 后端只允许删空课程：先把讲次移回未归类（视频与问答不受影响），再删课程
      await Promise.all(lectures.map((v) => setVideoCourse(v.id, null)))
      await deleteCourse(id)
      await refresh()
      notify(lectures.length ? `已删除课程，${lectures.length} 讲回到「未归类」` : '已删除课程')
      navigate('/')
    } catch (err) {
      setDeleteError(errorText(err))
      void refresh()
    } finally {
      setDeleting(false)
    }
  }

  const exportCourse = async () => {
    if (exportBusy.current) return
    exportBusy.current = true
    setExporting(true)
    setExportProgress(null)
    setExportError(null)
    setMenuOpen(false)
    let token: string | null = null
    try {
      const prepared = await prepareCoursePackage(id, setExportProgress)
      token = prepared.token
      const link = document.createElement('a')
      link.href = prepared.download_url
      link.download = prepared.filename
      document.body.appendChild(link)
      link.click()
      link.remove()
      notify('整门课程包已交给浏览器下载，可在下载列表查看进度')
    } catch (err) {
      if (token) await deletePackage(token).catch(() => {})
      setExportError(errorText(err))
    } finally {
      exportBusy.current = false
      setExporting(false)
    }
  }

  const onDrop = (e: DragEvent) => {
    e.preventDefault()
    setDragOver(false)
    const files = Array.from(e.dataTransfer.files ?? [])
    if (files.length) openImport(id, files)
  }

  if (error && courses === null) return <div className="page-center"><p className="error-text">加载失败：{error}</p><button className="btn" onClick={() => void refresh()}>重试</button><Link to="/">返回课程库</Link></div>
  if (courses === null || videos === null) return <div className="page-center"><p className="muted">加载中…</p></div>
  if (!course) return <div className="page-center"><p>课程不存在或已被删除。</p><Link to="/">返回课程库</Link></div>

  const total = lectures.reduce((s, v) => s + (v.duration_s ?? 0), 0)
  const done = lectures.filter((v) => v.status === 'done').length
  const pending = lectures.filter((v) => v.status === 'awaiting_roi' || v.status === 'awaiting_confirm' || v.status === 'failed').length

  return (
    <div className="lt-page">
      <nav className="lt-crumbs" aria-label="位置"><Link to="/">课程库</Link><span aria-hidden>/</span><span>{course.title}</span></nav>
      <header className="lt-page-head">
        <div className="lt-page-title">
          {editing ? (
            <form className="lt-inline-form" onSubmit={(e) => void saveName(e)}>
              <input className="lt-title-input" autoFocus aria-label="课程名称" value={name} maxLength={120} onChange={(e) => setName(e.target.value)}
                onKeyDown={(e) => e.key === 'Escape' && (setEditing(false), setNameError(null))} onBlur={(e) => { if (!e.currentTarget.form?.contains(e.relatedTarget as Node)) void saveName(e as unknown as FormEvent) }} />
              <button className="btn btn-primary" type="submit">保存</button>
            </form>
          ) : (
            <h1>
              {course.title}
              <button className="icon-btn lt-edit" aria-label="重命名课程" title="重命名" onClick={() => { setName(course.title); setEditing(true) }}>✎</button>
            </h1>
          )}
          {nameError && <p className="error-text small" role="alert">{nameError}</p>}
          <p className="muted">
            {lectures.length} 讲{total > 0 ? ` · 共 ${formatTime(total)}` : ''}
            {lectures.length > 0 && <> · {done} 已完成</>}
            {pending > 0 && <> · <strong className="lt-warn-text">{pending} 讲需要你处理</strong></>}
          </p>
        </div>
        <button className="btn btn-primary" onClick={() => openImport(id)}>导入到本课程</button>
        <div className="lt-menu-wrap" ref={menuRef}>
          <button className="btn" aria-label="课程更多操作" aria-haspopup="menu" aria-expanded={menuOpen} onClick={() => setMenuOpen((v) => !v)}>⋯</button>
          {menuOpen && (
            <div className="lt-menu" role="menu">
              <button role="menuitem" onClick={() => { setMenuOpen(false); setName(course.title); setEditing(true) }}>重命名</button>
              <button role="menuitem" disabled={exporting || lectures.length === 0} onClick={() => void exportCourse()}>导出整门课</button>
              <button role="menuitem" className="is-danger" onClick={() => { setMenuOpen(false); setDeleteError(null); setDeleteOpen(true) }}>删除课程…</button>
            </div>
          )}
        </div>
      </header>

      {error && <div className="alert alert-error">刷新失败：{error}</div>}
      {exporting && <PackageExportProgress progress={exportProgress} />}
      {exportError && <p className="error-text" role="alert">导出失败：{exportError}</p>}

      {lectures.length === 0 ? (
        <p className="muted empty">这门课程还没有讲次。</p>
      ) : (
        <LectureTable videos={lectures} courses={courses} currentCourseId={id} numbered onDelete={setToDelete} />
      )}

      <div
        className={`lt-dropline${dragOver ? ' is-over' : ''}`}
        onDragOver={(e) => { if (e.dataTransfer.types.includes('Files')) { e.preventDefault(); setDragOver(true) } }}
        onDragLeave={() => setDragOver(false)}
        onDrop={onDrop}
      >
        <span>把 MP4 拖到这里，直接成为本课程的下一讲</span>
        {inboxCount > 0 && <Link className="btn" to="/inbox">从未归类添加（{inboxCount}）</Link>}
      </div>

      <ConfirmDialog
        open={deleteOpen}
        title="删除课程"
        danger
        confirmText="删除课程"
        busy={deleting}
        error={deleteError}
        message={<p>删除「{course.title}」？{lectures.length > 0 ? `其中的 ${lectures.length} 讲不会被删除，会回到「未归类」，大纲、字幕和问答都保留。` : '这是一门空课程。'}</p>}
        onConfirm={() => void removeCourse()}
        onCancel={() => !deleting && setDeleteOpen(false)}
      />
      <DeleteVideosDialog videos={toDelete} onClose={() => setToDelete(null)} onDone={() => void refresh()} />
    </div>
  )
}
