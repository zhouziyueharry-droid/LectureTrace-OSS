import { useState } from 'react'
import { deleteVideo, errorText, type Video } from '../api.ts'
import { ConfirmDialog } from './Modal.tsx'

export default function DeleteVideosDialog({ videos, onClose, onDone }: { videos: Video[] | null; onClose: () => void; onDone: () => void }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const run = async () => {
    if (!videos) return
    setBusy(true)
    setError(null)
    const results = await Promise.allSettled(videos.map((v) => deleteVideo(v.id)))
    const failed = results.filter((r) => r.status === 'rejected')
    setBusy(false)
    onDone()
    if (failed.length === 0) onClose()
    else setError(`${failed.length} 个删除失败：${errorText((failed[0] as PromiseRejectedResult).reason)}`)
  }

  const one = videos?.length === 1 ? videos[0] : null
  return (
    <ConfirmDialog
      open={videos !== null && videos.length > 0}
      title="删除视频"
      message={
        <p>
          {one ? `确定删除「${one.title || one.filename}」吗？` : `确定删除选中的 ${videos?.length ?? 0} 个视频吗？`}
          视频文件、字幕、大纲和问答记录都会被删除，无法恢复。
        </p>
      }
      confirmText="删除"
      danger
      busy={busy}
      error={error}
      onConfirm={() => void run()}
      onCancel={() => !busy && (setError(null), onClose())}
    />
  )
}
