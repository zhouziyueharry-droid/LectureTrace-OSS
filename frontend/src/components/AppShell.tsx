import { useState, type DragEvent, type FormEvent } from 'react'
import { NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom'
import { createCourse, errorText } from '../api.ts'
import { useLibrary } from '../library.tsx'
import ThemeToggle from './ThemeToggle.tsx'

export const DRAG_TYPE = 'application/x-lt-videos'

export default function AppShell() {
  const { courses, videos, refresh, openImport, moveVideos } = useLibrary()
  const navigate = useNavigate()
  const isHome = useLocation().pathname === '/'
  const [navOpen, setNavOpen] = useState(false)
  const [adding, setAdding] = useState(false)
  const [title, setTitle] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [dropKey, setDropKey] = useState<string | null>(null)

  const countOf = (courseId: string | null) => videos?.filter((v) => (v.course_id ?? null) === courseId).length ?? 0
  const processing = (courseId: string) => videos?.some((v) => v.course_id === courseId && v.status === 'processing') ?? false
  const inboxCount = countOf(null)

  const create = async (e: FormEvent) => {
    e.preventDefault()
    if (!title.trim() || busy) return
    setBusy(true)
    setError(null)
    try {
      const c = await createCourse(title)
      setTitle('')
      setAdding(false)
      await refresh()
      setNavOpen(false)
      navigate(`/courses/${encodeURIComponent(c.id)}`)
    } catch (err) {
      setError(errorText(err))
    } finally {
      setBusy(false)
    }
  }

  const dropProps = (key: string, courseId: string | null) => ({
    onDragOver: (e: DragEvent) => {
      if (!e.dataTransfer.types.includes(DRAG_TYPE)) return
      e.preventDefault()
      setDropKey(key)
    },
    onDragLeave: () => setDropKey((k) => (k === key ? null : k)),
    onDrop: (e: DragEvent) => {
      const raw = e.dataTransfer.getData(DRAG_TYPE)
      setDropKey(null)
      if (!raw) return
      e.preventDefault()
      try {
        void moveVideos(JSON.parse(raw) as string[], courseId)
      } catch {
        /* 无效拖拽数据，忽略 */
      }
    },
  })

  const link = (to: string, label: string, count: number | null, o: { end?: boolean; dropKey?: string; courseId?: string | null; busy?: boolean; warn?: boolean } = {}) => (
    <NavLink
      key={to}
      to={to}
      end={o.end}
      onClick={() => setNavOpen(false)}
      className={({ isActive }) => `lt-nav-item${isActive ? ' is-active' : ''}${o.dropKey && dropKey === o.dropKey ? ' is-drop' : ''}`}
      {...(o.dropKey ? dropProps(o.dropKey, o.courseId ?? null) : {})}
    >
      <span className="lt-nav-label ellipsis" title={label}>{label}</span>
      {o.busy && <span className="lt-dot" role="img" aria-label="有视频正在处理" />}
      {o.dropKey && dropKey === o.dropKey ? (
        <span className="lt-nav-drop">移到这里</span>
      ) : (
        count !== null && <span className={`lt-count${o.warn && count > 0 ? ' is-warn' : ''}`}>{count} 讲</span>
      )}
    </NavLink>
  )

  return (
    <div className="lt-app">
      <a className="lt-skip" href="#main-content">跳到主内容</a>
      <aside className={`lt-side${navOpen ? ' is-open' : ''}`}>
        <div className="lt-brand-row">
          <NavLink to="/" className="lt-brand" onClick={() => setNavOpen(false)}>
            <span className="brand-mark" aria-hidden>▶</span>
            LectureTrace
          </NavLink>
          <ThemeToggle />
          <button className="btn lt-nav-toggle" aria-expanded={navOpen} aria-controls="library-navigation" onClick={() => setNavOpen(!navOpen)}>课程导航</button>
        </div>
        <div className="lt-side-body" id="library-navigation">
        {!isHome && <button className="btn lt-side-import" onClick={() => openImport(null)}>导入视频</button>}
        <nav className="lt-nav" aria-label="课程导航">
          {link('/', '课程库', videos?.length ?? null, { end: true })}
          <div className="lt-nav-head">
            <span>课程</span>
            <button className="lt-icon-sm" disabled={busy} aria-label="新建课程" onClick={() => setAdding((v) => !v)}>＋</button>
          </div>
          {adding && (
            <form className="lt-nav-new" onSubmit={(e) => void create(e)}>
              <input autoFocus disabled={busy} aria-label="新课程名称" placeholder="课程名称" maxLength={120} value={title}
                onChange={(e) => setTitle(e.target.value)}
                onKeyDown={(e) => !busy && e.key === 'Escape' && (setAdding(false), setTitle(''), setError(null))} />
              {error && <span className="error-text small" role="alert">{error}</span>}
              <div className="lt-inline-form">
                <button className="btn" type="submit" disabled={busy || !title.trim()}>{busy ? '创建中…' : '创建'}</button>
                <button className="btn" type="button" disabled={busy} onClick={() => { setAdding(false); setTitle(''); setError(null) }}>取消</button>
              </div>
            </form>
          )}
          {courses === null && <span className="muted small lt-nav-note">加载中…</span>}
          {courses?.length === 0 && !adding && <span className="muted small lt-nav-note">还没有课程</span>}
          {courses?.map((c) =>
            link(`/courses/${encodeURIComponent(c.id)}`, c.title, countOf(c.id), { dropKey: c.id, courseId: c.id, busy: processing(c.id) }),
          )}
          <div className="lt-nav-sep" />
          {link('/inbox', '未归类', inboxCount, { dropKey: 'inbox', courseId: null, warn: true })}
        </nav>
        <div className="lt-side-foot">
          {link('/settings', '设置', null)}
        </div>
        </div>
      </aside>
      <main className="lt-content" id="main-content" tabIndex={-1}>
        <Outlet />
      </main>
    </div>
  )
}
