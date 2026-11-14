import { systemText, tr, useLocale } from '../i18n.ts'
import { useEffect, useMemo, useState, type FormEvent } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { createCourse, errorText, formatTime, videoStatusLabel, type Video } from '../api.ts'
import DeleteVideosDialog from '../components/DeleteVideosDialog.tsx'
import LectureTable from '../components/LectureTable.tsx'
import { useLibrary } from '../library.tsx'

const ATTENTION: Record<string, string> = {
  get awaiting_roi() { return tr("字幕已完成，框选幻灯片区域后才开始抽帧") },
  get awaiting_confirm() { return tr("核对发送清单与费用预估后才会继续") },
  get failed() { return tr("处理中断，打开后可从失败的步骤重试") },
  get processing() { return tr("正在本机处理") },
}

export default function HomePage() {
  useLocale()
  const { courses, videos, error, refresh, openImport } = useLibrary()
  const navigate = useNavigate()
  const [query, setQuery] = useState('')
  const [adding, setAdding] = useState(false)
  const [title, setTitle] = useState('')
  const [creating, setCreating] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)
  const [toDelete, setToDelete] = useState<Video[] | null>(null)

  useEffect(() => {
    document.title = 'LectureTrace'
  }, [])

  const attention = useMemo(
    () => (videos ?? []).filter((v) => v.status !== 'done').sort((a, b) => rank(a.status) - rank(b.status)),
    [videos],
  )
  const needAction = attention.filter((v) => v.status !== 'processing').length
  const q = query.trim().toLowerCase()
  const hits = q ? (videos ?? []).filter((v) => `${v.title} ${v.filename} ${courses?.find((c) => c.id === v.course_id)?.title ?? ''}`.toLowerCase().includes(q)) : []

  const create = async (e: FormEvent) => {
    e.preventDefault()
    if (!title.trim() || creating) return
    setCreating(true)
    setFormError(null)
    try {
      const c = await createCourse(title)
      await refresh()
      navigate(`/courses/${encodeURIComponent(c.id)}`)
    } catch (err) {
      setFormError(errorText(err))
    } finally {
      setCreating(false)
    }
  }

  const inbox = videos?.filter((v) => !v.course_id) ?? []

  return (
    <div className="lt-page lt-home">
      <header className="lt-page-head">
        <div className="lt-page-title">
          <h1>{tr("课程库")}</h1>
          <p className="muted">
            {courses && videos ? tr("{0} 门课程 · {1} 讲", courses.length, videos.length) : tr("正在加载课程…")}
            {needAction > 0 && <> · <strong className="lt-warn-text">{needAction} {tr("讲需要你处理")}</strong></>}
          </p>
        </div>
        <button className="btn btn-primary" onClick={() => openImport(null)}>{tr("导入视频")}</button>
      </header>
      <input className="lt-search" type="search" aria-label={tr("搜索课程或讲次")} placeholder={tr("搜索课程、讲次或文件名")} value={query} onChange={(e) => setQuery(e.target.value)} />

      {error && <div className="alert alert-error" role="alert">{tr("加载失败：")}{systemText(error)} <button className="btn btn-small" onClick={() => void refresh()}>{tr("重试")}</button></div>}
      {(courses === null || videos === null) && !error && <p className="muted">{tr("加载中…")}</p>}

      {q ? (
        <section aria-label={tr("搜索结果")} className="lt-section">
          <h2>{tr("搜索结果 ·")} {hits.length}</h2>
          {hits.length === 0 ? <p className="muted empty">{tr("没有匹配「")}{query.trim()}{tr("」的讲次。")}</p> : (
            <div className="lt-results-scroll" role="region" aria-label={tr("讲次列表，可横向滚动")} tabIndex={0}>
              <LectureTable videos={hits} courses={courses ?? []} showCourse onDelete={setToDelete} />
            </div>
          )}
        </section>
      ) : (
        videos && courses && (
          <>
            {attention.length > 0 && (
              <section aria-labelledby="att-title" className="lt-section">
                <h2 id="att-title">{tr("需要处理")}</h2>
                <div className="lt-attn">
                  {attention.slice(0, 6).map((v) => (
                    <Link key={v.id} to={`/videos/${encodeURIComponent(v.id)}`} className="lt-attn-card">
                      <span className="lt-attn-top">
                        <span className={`status-pill st-${v.status}`}>{videoStatusLabel(v.status)}</span>
                        <span className="muted small ellipsis">{courses.find((c) => c.id === v.course_id)?.title ?? tr("未归类")}</span>
                      </span>
                      <strong className="ellipsis">{v.title || v.filename}</strong>
                      <span className="muted small">{ATTENTION[v.status]}</span>
                    </Link>
                  ))}
                </div>
                {attention.length > 6 && <p className="muted small">{tr("还有")} {attention.length - 6} {tr("讲未显示，进入对应课程查看。")}</p>}
              </section>
            )}

            <section aria-labelledby="courses-title" className="lt-section">
              <div className="lt-section-head">
                <h2 id="courses-title">{tr("我的课程")}</h2>
                <button className="btn" disabled={adding} onClick={() => setAdding(true)}>{tr("新建课程")}</button>
              </div>
              {courses.length === 0 && !adding && <p className="muted">{tr("导入第一讲开始学习，也可以先新建课程来整理视频。")}</p>}
              <div className="lt-cards">
                {courses.map((c) => {
                  const vs = videos.filter((v) => v.course_id === c.id)
                  const total = vs.reduce((s, v) => s + (v.duration_s ?? 0), 0)
                  const latest = [...vs].sort((a, b) => b.created_at.localeCompare(a.created_at))[0]
                  return (
                    <article key={c.id} className="lt-card lt-course-card">
                      <Link to={`/courses/${encodeURIComponent(c.id)}`} className="lt-course-link">
                        <h3 className="lt-card-title">{c.title}</h3>
                        <span className="muted">{vs.length} {tr("讲")}{total > 0 ? tr(" · 共 {0}", formatTime(total)) : ''}</span>
                        <span className="lt-course-enter">{tr("查看课程")}</span>
                      </Link>
                      <p className="lt-course-status muted small">{tr("已处理")}{vs.filter((v) => v.status === 'done').length} / {vs.length}{tr("讲")}{vs.some((v) => v.status !== 'done') && tr(" · 其余讲次见上方「需要处理」")}
                      </p>
                      <div className="lt-card-last">
                        {latest ? <>
                          <span className="muted small">{tr("最近导入")}</span>
                          <Link className="lt-latest-link" to={`/videos/${encodeURIComponent(latest.id)}`}>
                            <span>{latest.title || latest.filename}</span><span className="small">{tr("打开讲次")}</span>
                          </Link>
                        </> : <span className="muted small">{tr("还没有讲次，进入课程后导入第一讲。")}</span>}
                      </div>
                    </article>
                  )
                })}
                {inbox.length > 0 && (
                  <Link to="/inbox" className="lt-card is-inbox">
                    <strong className="lt-card-title">{tr("未归类")}</strong>
                    <span className="small">{inbox.length} {tr("讲还没有课程")}</span>
                    <span className="lt-card-last small"><strong>{tr("逐个整理 →")}</strong></span>
                  </Link>
                )}
                {adding ? (
                  <form className="lt-card lt-card-new" aria-busy={creating} onSubmit={(e) => void create(e)}>
                    <label className="field"><span>{tr("新课程名称")}</span>
                      <input autoFocus disabled={creating} placeholder={tr("例如 AI6103")} maxLength={120} value={title} onChange={(e) => setTitle(e.target.value)}
                        onKeyDown={(e) => !creating && e.key === 'Escape' && (setAdding(false), setFormError(null))} /></label>
                    {formError && <span className="error-text small" role="alert">{systemText(formError)}</span>}
                    <span className="lt-inline-form">
                      <button className="btn btn-primary" type="submit" disabled={!title.trim() || creating}>{creating ? tr("创建中…") : tr("创建")}</button>
                      <button className="btn" type="button" disabled={creating} onClick={() => setAdding(false)}>{tr("取消")}</button>
                    </span>
                  </form>
                ) : null}
              </div>
            </section>
          </>
        )
      )}

      <DeleteVideosDialog videos={toDelete} onClose={() => setToDelete(null)} onDone={() => void refresh()} />
    </div>
  )
}

const rank = (s: string) => ({ awaiting_roi: 0, awaiting_confirm: 0, failed: 1, processing: 2 } as Record<string, number>)[s] ?? 3
