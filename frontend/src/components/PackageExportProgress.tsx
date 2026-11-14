import { tr, useLocale } from '../i18n.ts'
import type { PackageProgress } from '../api.ts'

const size = (bytes: number) => bytes >= 1e9 ? `${(bytes / 1e9).toFixed(2)} GB` : bytes >= 1e6 ? `${(bytes / 1e6).toFixed(1)} MB` : `${(bytes / 1e3).toFixed(1)} KB`

export default function PackageExportProgress({ progress }: { progress: PackageProgress | null }) {
  useLocale()
  const phase = progress?.phase ?? 'waiting'
  const percent = progress?.total_bytes ? Math.floor(progress.written_bytes / progress.total_bytes * 100) : 0
  const label = { waiting: tr("等待开始打包…"), scanning: tr("正在统计课程文件…"), writing: tr("正在打包 · {0}%", percent), verifying: tr("文件已写入，正在校验课程包…") }[phase]
  return <div className="lt-package-export">
    <p className="small" role="status">{label}</p>
    <progress className="lt-package-progress" max={100} value={phase === 'waiting' || phase === 'scanning' ? undefined : percent} aria-label={tr("课程包打包进度")} />
    {!!progress?.total_bytes && <p className="muted small">{size(progress.written_bytes)} / {size(progress.total_bytes)}{progress.lecture_title && <> {tr("· 第")} {progress.lecture_index}/{progress.lecture_count} {tr("讲：")}{progress.lecture_title}</>}</p>}
    <p className="muted small">{tr("打包完成后开始下载；下载进度请在浏览器下载列表查看。")}</p>
  </div>
}
