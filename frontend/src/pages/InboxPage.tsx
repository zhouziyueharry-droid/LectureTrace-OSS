import { useEffect, useState } from 'react'
import { type Video } from '../api.ts'
import DeleteVideosDialog from '../components/DeleteVideosDialog.tsx'
import LectureTable from '../components/LectureTable.tsx'
import { useLibrary } from '../library.tsx'

export default function InboxPage() {
  const { courses, videos, error, refresh, openImport } = useLibrary()
  const [toDelete, setToDelete] = useState<Video[] | null>(null)
  useEffect(() => {
    document.title = '未归类 - LectureTrace'
  }, [])
  const list = videos?.filter((v) => !v.course_id) ?? []

  return (
    <div className="lt-page">
      <header className="lt-page-head">
        <div className="lt-page-title">
          <h1>未归类</h1>
          <p className="muted">{list.length} 讲还没有课程。勾选后可批量移入课程，也可以把一行拖到左侧的课程上。</p>
        </div>
        <button className="btn btn-primary" onClick={() => openImport(null)}>导入视频</button>
      </header>
      {error && <div className="alert alert-error">加载失败：{error}</div>}
      {videos === null && !error && <p className="muted">加载中…</p>}
      {videos && list.length === 0 && <p className="muted empty">没有未归类的视频，都整理好了。</p>}
      {videos && list.length > 0 && <LectureTable videos={list} courses={courses ?? []} currentCourseId={null} onDelete={setToDelete} />}
      <DeleteVideosDialog videos={toDelete} onClose={() => setToDelete(null)} onDone={() => void refresh()} />
    </div>
  )
}
