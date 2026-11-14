import { systemText, tr, useLocale } from './i18n.ts'
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { errorText, listCourses, listVideos, setVideoCourse, type Course, type Video } from './api.ts'
import ImportDialog from './components/ImportDialog.tsx'

interface Toast {
  id: number
  text: string
  undo?: () => void
  tone?: 'error'
}

interface ImportRequest {
  courseId: string | null
  files: File[]
}

interface Library {
  courses: Course[] | null
  videos: Video[] | null
  error: string | null
  refresh: () => Promise<void>
  /** 打开导入对话框；courseId 为默认归入的课程，files 为已拖入的文件 */
  openImport: (courseId?: string | null, files?: File[]) => void
  /** 批量移动讲次；courseId 为 null 表示移出课程。返回失败数量 */
  moveVideos: (ids: string[], courseId: string | null) => Promise<number>
  notify: (text: string, tone?: 'error') => void
}

const Ctx = createContext<Library | null>(null)

export function useLibrary(): Library {
  const v = useContext(Ctx)
  if (!v) throw new Error(tr("useLibrary 需要在 LibraryProvider 内使用"))
  return v
}

export function LibraryProvider({ children }: { children: ReactNode }) {
  useLocale()
  const [courses, setCourses] = useState<Course[] | null>(null)
  const [videos, setVideos] = useState<Video[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [importReq, setImportReq] = useState<ImportRequest | null>(null)
  const [toast, setToast] = useState<Toast | null>(null)
  const videosRef = useRef<Video[] | null>(null)
  const coursesRef = useRef<Course[] | null>(null)
  const toastTimer = useRef<number | undefined>(undefined)

  const refresh = useCallback(async () => {
    try {
      const [c, v] = await Promise.all([listCourses(), listVideos()])
      videosRef.current = v
      coursesRef.current = c
      setCourses(c)
      setVideos(v)
      setError(null)
    } catch (e) {
      setError(errorText(e))
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const anyProcessing = videos?.some((v) => v.status === 'processing') ?? false
  useEffect(() => {
    if (!anyProcessing) return
    const t = window.setInterval(() => void refresh(), 5000)
    return () => window.clearInterval(t)
  }, [anyProcessing, refresh])

  const notify = useCallback((text: string, tone?: 'error', undo?: () => void) => {
    window.clearTimeout(toastTimer.current)
    setToast({ id: Date.now(), text, undo, tone })
    toastTimer.current = window.setTimeout(() => setToast(null), undo ? 9000 : 5000)
  }, [])

  const moveVideos = useCallback(
    async (ids: string[], courseId: string | null) => {
      const prev = new Map((videosRef.current ?? []).map((v) => [v.id, v.course_id]))
      const todo = ids.filter((id) => (prev.get(id) ?? null) !== courseId)
      if (todo.length === 0) return 0
      const results = await Promise.allSettled(todo.map((id) => setVideoCourse(id, courseId)))
      const moved = todo.filter((_, i) => results[i].status === 'fulfilled')
      const failed = todo.length - moved.length
      await refresh()
      const target = courseId ? coursesRef.current?.find((c) => c.id === courseId)?.title ?? tr("课程") : tr("未归类")
      if (moved.length > 0) {
        notify(
          tr("已把 {0} 讲移到「{1}」{2}", moved.length, target, failed ? tr("，{0} 讲失败", failed) : ''),
          failed ? 'error' : undefined,
          async () => {
            await Promise.allSettled(moved.map((id) => setVideoCourse(id, prev.get(id) ?? null)))
            await refresh()
            setToast(null)
          },
        )
      } else {
        notify(tr("移动失败，请稍后重试"), 'error')
      }
      return failed
    },
    [refresh, notify],
  )

  const openImport = useCallback((courseId?: string | null, files?: File[]) => {
    setImportReq({ courseId: courseId ?? null, files: files ?? [] })
  }, [])

  const value = useMemo<Library>(
    () => ({ courses, videos, error, refresh, openImport, moveVideos, notify: (t, tone) => notify(t, tone) }),
    [courses, videos, error, refresh, openImport, moveVideos, notify],
  )

  return (
    <Ctx.Provider value={value}>
      {children}
      {importReq && (
        <ImportDialog
          initialCourseId={importReq.courseId}
          initialFiles={importReq.files}
          courses={courses ?? []}
          onClose={() => setImportReq(null)}
          onDone={() => void refresh()}
          onCourseCreated={() => void refresh()}
          notify={(t) => notify(t)}
        />
      )}
      {toast && (
        <div className={`lt-toast${toast.tone === 'error' ? ' is-error' : ''}`} role="status" key={toast.id}>
          <span>{systemText(toast.text)}</span>
          {toast.undo && (
            <button className="lt-toast-btn" onClick={toast.undo}>{tr("撤销")}</button>
          )}
          <button className="lt-toast-btn" aria-label={tr("关闭提示")} onClick={() => setToast(null)}>
            ×
          </button>
        </div>
      )}
    </Ctx.Provider>
  )
}
