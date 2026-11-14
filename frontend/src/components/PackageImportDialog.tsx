import { systemText, tr, useLocale } from '../i18n.ts'
import { useEffect, useRef, useState, type ReactNode } from 'react'
import { useNavigate } from 'react-router-dom'
import { deletePackage, errorText, importPackage, previewPackage, type PackagePreview } from '../api.ts'
import Modal from './Modal.tsx'

const formatSize = (bytes: number) => bytes >= 1e9 ? `${(bytes / 1e9).toFixed(1)} GB` : `${Math.max(1, Math.round(bytes / 1e6))} MB`

interface Props {
  initialFile?: File
  onClose: () => void
  onDone: () => void
  notify: (text: string) => void
  modes: (busy: boolean) => ReactNode
}

export default function PackageImportDialog({ initialFile, onClose, onDone, notify, modes }: Props) {
  useLocale()
  const navigate = useNavigate()
  const [file, setFile] = useState<File | null>(initialFile ?? null)
  const [preview, setPreview] = useState<PackagePreview | null>(null)
  const [phase, setPhase] = useState<'ready' | 'uploading' | 'importing' | 'cleaning'>('ready')
  const [progress, setProgress] = useState(0)
  const [error, setError] = useState<string | null>(null)
  const [dragOver, setDragOver] = useState(false)
  const input = useRef<HTMLInputElement>(null)
  const stagedToken = useRef<string | null>(null)
  const upload = useRef<ReturnType<typeof previewPackage> | null>(null)
  const uploaded = useRef(false)
  const active = useRef(true)
  const busy = phase !== 'ready'

  useEffect(() => {
    active.current = true
    return () => {
      active.current = false
      // Let server validation finish after upload, so its returned token can be deleted.
      if (!uploaded.current) upload.current?.abort()
      if (stagedToken.current) void deletePackage(stagedToken.current).catch(() => {})
    }
  }, [])

  const clearStaged = async () => {
    if (!stagedToken.current) return
    await deletePackage(stagedToken.current)
    stagedToken.current = null
    setPreview(null)
  }

  const choose = async (files: FileList | File[]) => {
    if (busy) return
    setError(null)
    if (files.length !== 1 || !/\.(ltcourse|zip)$/i.test(files[0].name)) {
      setError(tr("请一次选择一个 .ltcourse 或 .zip 课程包。"))
      return
    }
    // FileList is live: the input is reset by onChange before async cleanup ends.
    const selected = files[0]
    setPhase('cleaning')
    try {
      await clearStaged()
      setFile(selected)
      setProgress(0)
    } catch (e) {
      setError(tr("未能清理上一个课程包，请重试：{0}", errorText(e)))
    } finally {
      setPhase('ready')
    }
  }

  const inspect = async () => {
    if (!file || busy) return
    setPhase('uploading')
    setError(null)
    setProgress(0)
    uploaded.current = false
    try {
      const handle = previewPackage(file, (value) => {
        uploaded.current = value >= 1
        if (active.current) setProgress(value)
      })
      upload.current = handle
      const result = await handle.promise
      if (!active.current) {
        await deletePackage(result.token)
        return
      }
      stagedToken.current = result.token
      setPreview(result)
    } catch (e) {
      if (active.current) setError(errorText(e))
    } finally {
      upload.current = null
      if (active.current) setPhase('ready')
    }
  }

  const confirm = async () => {
    if (!preview || preview.duplicate || busy) return
    setPhase('importing')
    setError(null)
    try {
      const result = await importPackage(preview.token)
      stagedToken.current = null
      onDone()
      notify(tr("课程包已导入，已恢复已有结果，无需重新处理"))
      onClose()
      navigate('course_id' in result ? `/courses/${encodeURIComponent(result.course_id)}` : `/videos/${encodeURIComponent(result.video_id)}`)
    } catch (e) {
      setError(errorText(e))
      setPhase('ready')
    }
  }

  const close = async () => {
    if (phase === 'importing' || phase === 'cleaning') return
    if (phase === 'uploading') {
      onClose()
      return
    }
    setPhase('cleaning')
    try {
      await clearStaged()
      onClose()
    } catch (e) {
      setError(tr("未能清理暂存，请重试：{0}", errorText(e)))
      setPhase('ready')
    }
  }

  return (
    <Modal open title={tr("导入课程包")} width={620} onClose={() => void close()} footer={
      <>
        <button className="btn" disabled={phase === 'importing' || phase === 'cleaning'} onClick={() => void close()}>{phase === 'uploading' ? tr("取消上传") : tr("取消")}</button>
        <button className="btn btn-primary" disabled={busy || !file || preview?.duplicate} onClick={() => void (preview ? confirm() : inspect())}>
          {phase === 'importing' ? tr("正在恢复…") : phase === 'uploading' ? progress >= 1 ? tr("正在校验…") : tr("正在上传…") : preview ? tr("确认导入") : tr("上传并预览")}
        </button>
      </>
    }>
      <div className="lt-import">
        {modes(busy)}
        <p className="muted small">{tr("恢复单讲或整门课，保留各讲原视频、已有结果、笔记和 PDF。导入后不会自动运行模型。")}</p>
        <div className={`lt-drop${dragOver ? ' is-over' : ''}`} onDragOver={(event) => { event.preventDefault(); if (!busy) setDragOver(true) }} onDragLeave={() => setDragOver(false)} onDrop={(event) => { event.preventDefault(); setDragOver(false); void choose(event.dataTransfer.files) }}>
          <input ref={input} type="file" accept=".ltcourse,.zip" hidden onChange={(event) => { if (event.target.files?.length) void choose(event.target.files); event.target.value = '' }} />
          <strong>{tr("把一个课程包拖到这里")}</strong>
          <span className="muted small">.ltcourse / .zip · <button className="lt-linkbtn" disabled={busy} onClick={() => input.current?.click()}>{file ? tr("重新选择文件") : tr("选择文件")}</button></span>
        </div>
        {file && <div className="lt-import-item"><div className="lt-import-main"><span className="ellipsis" title={file.name}>{file.name}</span><span className="muted small">{formatSize(file.size)}</span></div></div>}
        {phase === 'uploading' && <div role="status" aria-live="polite">
          <p className="small">{progress >= 1 ? tr("上传完成，正在校验课程包；大文件可能需要几分钟。") : tr("正在上传 {0}%", Math.round(progress * 100))}</p>
          <progress className="lt-package-progress" value={progress} max={1} aria-label={tr("课程包上传进度")} />
        </div>}
        {preview && <section className="lt-package-preview" aria-label={tr("课程包预览")}>
          <h3>{preview.title}</h3>
          <dl>
            <dt>{tr("归入课程")}</dt><dd>{preview.course_title ?? tr("未归类")}</dd>
            {preview.lectures && <><dt>{tr("讲次")}</dt><dd>{preview.lectures.length} {tr("讲")}</dd></>}
            <dt>{tr("原视频")}</dt><dd>{tr("已包含，可直接播放")}</dd>
            <dt>{tr("包内数据")}</dt><dd>{formatSize(preview.size_bytes)}</dd>
            <dt>{tr("共享笔记")}</dt><dd>{preview.note_count} {tr("篇")}</dd>
          </dl>
          <p className="small">{tr("包含：")}{preview.contents.map(systemText).join(' / ') || tr("原视频")}</p>
          {preview.lectures && <ol aria-label={tr("包内讲次")}>{preview.lectures.map((lecture) => <li key={lecture.video_id}>{lecture.title}{lecture.duplicate && <span className="error-text"> {tr("· 本机已存在")}</span>}</li>)}</ol>}
          <p className="muted small">{tr("沿用包内课程归类；不会导入聊天记录、登录状态或 API 设置。")}</p>
          {preview.duplicate && <p className="error-text small" role="alert">{preview.lectures ? tr("包内部分或全部讲次已存在，无法导入整包。") : tr("本机已存在这节视频，无法重复导入。")}{tr("已有内容不会被覆盖，请选择其他课程包。")}</p>}
        </section>}
        {error && <p className="error-text small" role="alert">{systemText(error)}</p>}
      </div>
    </Modal>
  )
}
