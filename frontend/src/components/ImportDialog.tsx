import { useRef, useState, type DragEvent, type FormEvent, type ReactNode } from 'react'
import { useNavigate } from 'react-router-dom'
import { createCourse, errorText, setVideoCourse, uploadVideo, type Course, type UploadHandle } from '../api.ts'
import Modal from './Modal.tsx'
import PackageImportDialog from './PackageImportDialog.tsx'

interface Item {
  key: number
  file: File
  error: string | null
  state: 'ready' | 'uploading' | 'done' | 'failed'
  progress: number
  videoId?: string
}

interface Props {
  initialCourseId: string | null
  initialFiles: File[]
  courses: Course[]
  onClose: () => void
  onDone: () => void
  onCourseCreated: () => void
  notify: (text: string) => void
}

const isMp4 = (f: File) => /\.mp4$/i.test(f.name)
const fmtSize = (n: number) => (n >= 1e9 ? `${(n / 1e9).toFixed(1)} GB` : `${Math.max(1, Math.round(n / 1e6))} MB`)

let seq = 0
const toItem = (file: File): Item => ({
  key: ++seq,
  file,
  error: isMp4(file) ? null : '只支持 MP4 文件，已忽略',
  state: 'ready',
  progress: 0,
})

export default function ImportDialog(props: Props) {
  const [mode, setMode] = useState<'mp4' | 'package'>(() => props.initialFiles.length === 1 && /\.(ltcourse|zip)$/i.test(props.initialFiles[0].name) ? 'package' : 'mp4')
  const modes = (busy: boolean) => (
    <div className="segmented lt-import-modes" role="group" aria-label="导入类型">
      <button type="button" className={mode === 'mp4' ? 'is-on' : ''} aria-pressed={mode === 'mp4'} disabled={busy} onClick={() => setMode('mp4')}>导入 MP4</button>
      <button type="button" className={mode === 'package' ? 'is-on' : ''} aria-pressed={mode === 'package'} disabled={busy} onClick={() => setMode('package')}>导入已处理课程包</button>
    </div>
  )
  return mode === 'package'
    ? <PackageImportDialog initialFile={props.initialFiles.find((file) => /\.(ltcourse|zip)$/i.test(file.name))} onClose={props.onClose} onDone={props.onDone} modes={modes} notify={props.notify} />
    : <Mp4ImportDialog {...props} modes={modes} />
}

function Mp4ImportDialog({ initialCourseId, initialFiles, courses, onClose, onDone, onCourseCreated, notify, modes }: Props & { modes: (busy: boolean) => ReactNode }) {
  const navigate = useNavigate()
  const [items, setItems] = useState<Item[]>(() => initialFiles.map(toItem))
  const [courseId, setCourseId] = useState(initialCourseId ?? '')
  const [dragOver, setDragOver] = useState(false)
  const [running, setRunning] = useState(false)
  const [newCourse, setNewCourse] = useState<string | null>(null)
  const [courseBusy, setCourseBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [created, setCreated] = useState<Course[]>([])
  const fileInput = useRef<HTMLInputElement>(null)
  const handleRef = useRef<UploadHandle | null>(null)
  const cancelled = useRef(false)

  const patch = (key: number, p: Partial<Item>) => setItems((xs) => xs.map((x) => (x.key === key ? { ...x, ...p } : x)))
  const addFiles = (files: FileList | File[]) => {
    if (running) return
    setItems((xs) => [...xs, ...Array.from(files).map(toItem)])
  }
  const valid = items.filter((x) => !x.error && x.state !== 'done')

  const createNew = async (e: FormEvent) => {
    e.preventDefault()
    if (!newCourse?.trim()) return
    setCourseBusy(true)
    setError(null)
    try {
      const c = await createCourse(newCourse)
      setCourseId(c.id)
      setNewCourse(null)
      onCourseCreated()
      setCreated((xs) => [...xs, c])
    } catch (err) {
      setError(errorText(err))
    } finally {
      setCourseBusy(false)
    }
  }

  const start = async () => {
    setRunning(true)
    setError(null)
    cancelled.current = false
    const done: string[] = []
    for (const item of valid) {
      if (cancelled.current) break
      patch(item.key, { state: 'uploading', progress: 0 })
      try {
        const handle = uploadVideo(item.file, (p) => patch(item.key, { progress: p }))
        handleRef.current = handle
        const id = await handle.promise
        let note: string | null = null
        if (courseId) {
          try {
            await setVideoCourse(id, courseId)
          } catch (e) {
            note = `已导入，但未能归入课程：${errorText(e)}`
          }
        }
        patch(item.key, { state: 'done', progress: 1, videoId: id, error: note })
        done.push(id)
      } catch (e) {
        patch(item.key, { state: 'failed', error: cancelled.current ? '已取消' : errorText(e) })
      }
    }
    handleRef.current = null
    setRunning(false)
    onDone()
    if (done.length === 0) return
    if (done.length === 1 && valid.length === 1) {
      onClose()
      navigate(`/videos/${encodeURIComponent(done[0])}`)
    } else if (done.length === valid.length) {
      notify(`已导入 ${done.length} 个视频，正在本机处理`)
      onClose()
    }
  }

  const cancelAll = () => {
    cancelled.current = true
    handleRef.current?.abort()
  }

  const onDrop = (e: DragEvent) => {
    e.preventDefault()
    setDragOver(false)
    if (e.dataTransfer.files?.length) addFiles(e.dataTransfer.files)
  }

  const label = running ? '正在上传…' : valid.length > 0 ? `导入 ${valid.length} 个视频` : '导入'

  return (
    <Modal
      open
      title="导入视频"
      width={620}
      onClose={() => !running && onClose()}
      footer={
        <>
          {running ? (
            <button className="btn" onClick={cancelAll}>取消上传</button>
          ) : (
            <button className="btn" onClick={onClose}>关闭</button>
          )}
          <button className="btn btn-primary" disabled={running || valid.length === 0} onClick={() => void start()}>
            {label}
          </button>
        </>
      }
    >
      <div className="lt-import">
        {modes(running)}
        <div
          className={`lt-drop${dragOver ? ' is-over' : ''}`}
          onDragOver={(e) => {
            e.preventDefault()
            setDragOver(true)
          }}
          onDragLeave={() => setDragOver(false)}
          onDrop={onDrop}
        >
          <input ref={fileInput} type="file" accept=".mp4,video/mp4" multiple hidden
            onChange={(e) => {
              if (e.target.files) addFiles(e.target.files)
              e.target.value = ''
            }} />
          <strong>把 MP4 拖到这里，可一次放多个</strong>
          <span className="muted small">
            或 <button type="button" className="lt-linkbtn" onClick={() => fileInput.current?.click()} disabled={running}>选择文件</button>
          </span>
        </div>

        {items.length > 0 && (
          <ul className="lt-import-list" aria-label="待导入文件">
            {items.map((x) => (
              <li key={x.key} className={`lt-import-item${x.error && x.state !== 'done' ? ' is-bad' : ''}`}>
                <div className="lt-import-main">
                  <span className="ellipsis" title={x.file.name}>{x.file.name}</span>
                  <span className="small muted">
                    {x.state === 'done' ? '已导入' : x.state === 'uploading' ? `${Math.round(x.progress * 100)}%` : x.error ?? fmtSize(x.file.size)}
                    {x.state === 'done' && x.error ? ` · ${x.error}` : ''}
                  </span>
                  {x.state === 'uploading' && (
                    <div className="progress progress-thin"><div className="progress-bar" style={{ width: `${Math.round(x.progress * 100)}%` }} /></div>
                  )}
                </div>
                {x.state !== 'uploading' && x.state !== 'done' && (
                  <button className="icon-btn" aria-label={`移除 ${x.file.name}`} disabled={running}
                    onClick={() => setItems((xs) => xs.filter((y) => y.key !== x.key))}>×</button>
                )}
              </li>
            ))}
          </ul>
        )}

        <div className="lt-import-course">
          <label className="field">
            <span>归入课程</span>
            <select value={courseId} disabled={running} onChange={(e) => setCourseId(e.target.value)}>
              <option value="">未归类</option>
              {[...courses, ...created.filter((c) => !courses.some((x) => x.id === c.id))].map((c) => <option key={c.id} value={c.id}>{c.title}</option>)}
            </select>
          </label>
          {newCourse === null ? (
            <button className="btn" disabled={running} onClick={() => setNewCourse('')}>新建课程</button>
          ) : (
            <form className="lt-inline-form" onSubmit={(e) => void createNew(e)}>
              <input autoFocus aria-label="新课程名称" placeholder="例如 AI6103" maxLength={120} value={newCourse}
                onChange={(e) => setNewCourse(e.target.value)} onKeyDown={(e) => e.key === 'Escape' && (e.stopPropagation(), setNewCourse(null))} />
              <button className="btn btn-primary" type="submit" disabled={courseBusy || !newCourse.trim()}>创建</button>
            </form>
          )}
        </div>
        <p className="muted small">导入后依次上传，并自动开始本机处理（音频、语音识别、幻灯片）。上传期间请勿关闭页面。</p>
        {error && <p className="error-text small">{error}</p>}
      </div>
    </Modal>
  )
}
