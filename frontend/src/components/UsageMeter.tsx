import { systemText, getLocale, tr, useLocale } from '../i18n.ts'
import { useEffect, useRef, useState } from 'react'
import type { ContextSnapshot } from '../api.ts'
import { quotaWindows, useCodexQuota } from './QuotaStatus.tsx'

function tokens(value: unknown, compact = false) {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) return tr("未知")
  if (compact && value >= 1_000_000) return `${(value / 1_000_000).toFixed(1).replace(/\.0$/, '')}M`
  if (compact && value >= 1_000) return `${(value / 1_000).toFixed(1)}k`
  return value.toLocaleString(getLocale())
}

const level = (percent: number) => (percent >= 90 ? 'is-danger' : percent >= 75 ? 'is-warn' : '')

function resetText(at: number | null) {
  if (at === null) return tr("重置时间未知")
  const ms = at - Date.now()
  if (ms <= 0) return tr("即将重置")
  if (ms < 24 * 3600 * 1000) {
    const mins = Math.ceil(ms / 60000)
    return mins >= 60 ? tr("{0} 小时 {1} 分后重置", Math.floor(mins / 60), mins % 60) : tr("{0} 分后重置", mins)
  }
  return tr("{0} 重置", new Date(at).toLocaleString(getLocale(), { weekday: 'short', hour: '2-digit', minute: '2-digit' }))
}

function Bar({ percent, label }: { percent: number; label: string }) {
  useLocale()
  return (
    <div className={`usage-bar ${level(percent)}`} role="meter" aria-label={label} aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(percent)}>
      <span style={{ width: `${percent}%` }} />
    </div>
  )
}

interface Props {
  snapshot: ContextSnapshot | null
  busy: boolean
  /** 快照是否属于当前这一轮 */
  current: boolean
  compacting: boolean
}

const RECENT = [
  ['inputTokens', '输入'], ['cachedInputTokens', '其中缓存输入'], ['outputTokens', '输出'],
  ['reasoningOutputTokens', '其中推理输出'], ['totalTokens', '总量'],
] as const
const CUMULATIVE = [
  ['input_tokens', '累计输入'], ['cached_input_tokens', '其中缓存输入'], ['output_tokens', '累计输出'],
  ['reasoning_tokens', '其中推理输出'], ['tool_calls', '读取调用'], ['read_tokens', '返回文字 token'], ['images', '截图'],
] as const

/** 输入框旁的用量圆环；点开后显示上下文窗口和 ChatGPT 套餐额度 */
export default function UsageMeter({ snapshot, busy, current, compacting }: Props) {
  useLocale()
  const [open, setOpen] = useState(false)
  const root = useRef<HTMLDivElement>(null)
  const quota = useCodexQuota(busy)

  useEffect(() => {
    if (!open) return
    const onDown = (e: PointerEvent) => {
      if (!root.current?.contains(e.target as Node)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('pointerdown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('pointerdown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  const capacity = snapshot?.context_window ?? null
  const last = snapshot?.last
  const used = last?.totalTokens ?? null
  const hasPercent = used !== null && capacity !== null && capacity > 0
  const percent = hasPercent ? Math.min(100, (used / capacity) * 100) : null
  const share = (n: number | null | undefined) =>
    hasPercent && typeof n === 'number' && n > 0 ? Math.min(100, (n / capacity) * 100) : 0
  const cached = last?.cachedInputTokens ?? 0
  const freshInput = Math.max(0, (last?.inputTokens ?? 0) - cached)
  const status = compacting ? tr("正在压缩上下文") : busy ? (current ? tr("实时统计") : tr("等待本轮统计")) : snapshot ? tr("最近统计") : tr("下次提问后更新")

  const windows = quotaWindows(quota.status?.rate_limits ?? null)
  const plan = quota.status?.account?.planType
  const worst = Math.max(percent ?? 0, ...windows.map((w) => w.usedPercent ?? 0))

  const R = 8
  const C = 2 * Math.PI * R

  return (
    <div className="usage-meter" ref={root}>
      <button
        type="button"
        className={`usage-ring ${level(worst)}`}
        aria-label={percent !== null ? tr("用量与上下文窗口，上下文已用 {0}%", percent.toFixed(0)) : tr("用量与上下文窗口")}
        aria-expanded={open}
        aria-haspopup="dialog"
        onClick={() => setOpen((v) => !v)}
      >
        <svg width="22" height="22" viewBox="0 0 22 22" aria-hidden="true">
          <circle cx="11" cy="11" r={R} className="usage-ring-track" fill="none" stroke="currentColor" strokeOpacity={0.2} strokeWidth={3} />
          <circle cx="11" cy="11" r={R} className="usage-ring-fill" fill="none" stroke="currentColor" strokeWidth={3} strokeLinecap="round" strokeDasharray={C} strokeDashoffset={C * (1 - (percent ?? 0) / 100)} transform="rotate(-90 11 11)" />
        </svg>
        <span className="usage-ring-text">{percent !== null ? `${Math.round(percent)}%` : tr("用量")}</span>
      </button>

      {open && (
        <div className="usage-pop" role="dialog" aria-label={tr("用量与上下文窗口")}>
          <section>
            <div className="usage-row">
              <span className="usage-title">{tr("上下文窗口")}</span>
              <span className="usage-value">
                {snapshot
                  ? `${tokens(used, true)} / ${capacity !== null && capacity > 0 ? tokens(capacity, true) : tr("容量未知")}${percent !== null ? ` (${percent.toFixed(0)}%)` : ''}`
                  : tr("等待统计")}
              </span>
            </div>
            <div className="usage-bar usage-bar-multi" role="meter" aria-label={tr("上下文窗口占用估计")} aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(percent ?? 0)}>
              <span className="seg-cached" style={{ width: `${share(cached)}%` }} />
              <span className="seg-input" style={{ width: `${share(freshInput)}%` }} />
              <span className="seg-output" style={{ width: `${share(last?.outputTokens)}%` }} />
            </div>
            <div className="usage-legend">
              <span><i className="seg-cached" />{tr("缓存输入")}</span>
              <span><i className="seg-input" />{tr("新输入")}</span>
              <span><i className="seg-output" />{tr("输出")}</span>
              <span className="usage-muted">{status}</span>
            </div>
          </section>

          <section>
            <div className="usage-row">
              <span className="usage-title usage-muted">{tr("套餐用量限制")}{plan ? ` · ${plan}` : ''}</span>
              <button type="button" className="usage-link" disabled={busy || quota.loading} onClick={quota.reload}>
                {quota.loading && !busy ? tr("更新中…") : tr("刷新")}
              </button>
            </div>
            {windows.length === 0 && !quota.error && <p className="usage-muted usage-note">{quota.loading ? tr("读取中…") : tr("暂无额度信息")}</p>}
            {windows.map((w) => (
              <div key={w.key} className="usage-window">
                <div className="usage-row">
                  <span className="usage-title">{w.label}</span>
                  <span className="usage-value usage-muted">
                    {resetText(w.resetsAt)}
                    {w.usedPercent !== null ? <b> {Math.round(w.usedPercent)}%</b> : tr(" 未知")}
                  </span>
                </div>
                {w.usedPercent !== null && <Bar percent={w.usedPercent} label={tr("{0}已用", w.label)} />}
              </div>
            ))}
            {quota.error && <p className="error-text usage-note">{tr("额度读取失败：")}{systemText(quota.error)}</p>}
            <p className="usage-muted usage-note">{tr("账户共享额度；打开页面和提问结束后更新。")}</p>
          </section>

          <details className="usage-details">
            <summary>{tr("详细统计")}</summary>
            <p className="usage-muted usage-note">
              {snapshot?.model ?? 'Codex'} {tr("· 容量")} {tokens(capacity)}{tr("token。占用是最近一次模型交互的近似值，不是当前请求发送前的测量。")}</p>
            <div className="usage-columns">
              <div>
                <h4>{tr("最近一次交互")}</h4>
                <dl>{RECENT.map(([key, label]) => <div key={key}><dt>{tr(label)}</dt><dd>{tokens(snapshot?.last[key])}</dd></div>)}</dl>
              </div>
              <div>
                <h4>{busy && !current ? tr("上次提问") : tr("本轮提问")}</h4>
                <dl>{CUMULATIVE.map(([key, label]) => <div key={key}><dt>{tr(label)}</dt><dd>{tokens(snapshot?.usage[key])}</dd></div>)}</dl>
              </div>
            </div>
            <p className="usage-muted usage-note">{tr("缓存包含在输入中；推理包含在输出中。工具读取统计与模型用量不相加。")}</p>
          </details>
        </div>
      )}
    </div>
  )
}
