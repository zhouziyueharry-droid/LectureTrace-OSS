import { getLocale, tr } from '../i18n.ts'
import { useEffect, useState } from 'react'
import { errorText, getCodexStatus, type CodexStatus } from '../api.ts'

export function quotaText(limits: Record<string, unknown> | null, detailed = true): string {
  const map = limits?.rateLimitsByLimitId
  const buckets = map && typeof map === 'object' && Object.keys(map).length
    ? Object.entries(map) : [['', limits?.rateLimits]]
  const windows: string[] = []
  for (const [id, value] of buckets) {
    if (!value || typeof value !== 'object') continue
    const bucket = value as Record<string, unknown>
    for (const key of ['primary', 'secondary']) {
      const value = bucket[key]
      if (!value || typeof value !== 'object') continue
      const row = value as Record<string, unknown>
      const used = row.usedPercent
      const mins = row.windowDurationMins
      const reset = row.resetsAt
      const label = mins === 10080 ? tr("每周") : typeof mins === 'number' && Number.isFinite(mins) && mins > 0
        ? mins % 60 === 0 ? tr("{0} 小时", mins / 60) : tr("{0} 分钟", mins) : tr("窗口")
      const remaining = typeof used === 'number' && Number.isFinite(used)
        ? `${Math.max(0, Math.min(100, 100 - used))}%` : tr("未知")
      const date = typeof reset === 'number' && Number.isFinite(reset) ? new Date(reset * 1000) : null
      windows.push(tr("{0}{1}剩余 {2}{3}", buckets.length > 1 ? `${bucket.limitName ?? id} · ` : '', label, remaining, detailed && date && Number.isFinite(date.getTime()) ? tr("，{0} 重置", date.toLocaleString(getLocale())) : ''))
    }
  }
  return tr("套餐余量：{0}", windows.length ? windows.join(tr("；")) : tr("未知"))
}

export interface QuotaWindow {
  key: string
  label: string
  /** 已用百分比 0–100；未知为 null */
  usedPercent: number | null
  /** 重置时间（毫秒时间戳）；未知为 null */
  resetsAt: number | null
}

const windowLabel = (mins: unknown) => {
  if (mins === 10080) return tr("每周限额")
  if (typeof mins !== 'number' || !Number.isFinite(mins) || mins <= 0) return tr("用量限额")
  return mins % 60 === 0 ? tr("{0} 小时限额", mins / 60) : tr("{0} 分钟限额", mins)
}

/** 把运行时上报的额度窗口整理成列表；有多个限额桶时在名称前加桶名 */
export function quotaWindows(limits: Record<string, unknown> | null): QuotaWindow[] {
  const map = limits?.rateLimitsByLimitId
  const buckets = map && typeof map === 'object' && Object.keys(map).length
    ? Object.entries(map) : [['', limits?.rateLimits]]
  const rows: QuotaWindow[] = []
  for (const [id, value] of buckets) {
    if (!value || typeof value !== 'object') continue
    const bucket = value as Record<string, unknown>
    for (const key of ['primary', 'secondary']) {
      const w = bucket[key]
      if (!w || typeof w !== 'object') continue
      const row = w as Record<string, unknown>
      const used = row.usedPercent
      const reset = row.resetsAt
      const name = buckets.length > 1 ? `${bucket.limitName ?? id} · ` : ''
      rows.push({
        key: `${id}:${key}`,
        label: `${name}${windowLabel(row.windowDurationMins)}`,
        usedPercent: typeof used === 'number' && Number.isFinite(used) ? Math.max(0, Math.min(100, used)) : null,
        resetsAt: typeof reset === 'number' && Number.isFinite(reset) ? reset * 1000 : null,
      })
    }
  }
  return rows
}

/** 读取 ChatGPT 套餐额度：打开页面和每次提问结束后更新 */
export function useCodexQuota(busy: boolean) {
  const [status, setStatus] = useState<CodexStatus | null>(null)
  const [refresh, setRefresh] = useState(0)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    if (busy) return
    let active = true
    setLoading(true)
    getCodexStatus().then(
      (result) => { if (active) { setStatus(result); setError(result.error) } },
      (e) => { if (active) { setStatus(null); setError(errorText(e)) } },
    ).finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [busy, refresh])
  return { status, loading, error, reload: () => setRefresh((v) => v + 1) }
}
