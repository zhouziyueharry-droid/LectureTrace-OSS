import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { formatTime, type KeyframeDebug } from '../api.ts'

/** 每张幻灯片一个颜色（按序号循环），明暗主题都能看清 */
const HUES = [212, 150, 32, 280, 350, 190, 95, 250]
export function slideColor(index: number, alpha = 1): string {
  return `hsla(${HUES[index % HUES.length]}, 65%, 55%, ${alpha})`
}

interface Props {
  debug: KeyframeDebug
  /** 幻灯片 ID → 序号（决定颜色） */
  slideIndex: Map<string, number>
  duration: number
  windowStart: number
  span: number
  currentTime: number
  onWindowStart: (t: number) => void
  onHover: (t: number | null) => void
  onSeek: (t: number) => void
}

function useCanvas(height: number) {
  const ref = useRef<HTMLCanvasElement>(null)
  const [width, setWidth] = useState(0)
  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return
    const ro = new ResizeObserver(() => setWidth(el.clientWidth))
    ro.observe(el)
    setWidth(el.clientWidth)
    return () => ro.disconnect()
  }, [])
  const getCtx = useCallback(() => {
    const el = ref.current
    if (!el || width === 0) return null
    const dpr = window.devicePixelRatio || 1
    el.width = Math.round(width * dpr)
    el.height = Math.round(height * dpr)
    const ctx = el.getContext('2d')
    ctx?.setTransform(dpr, 0, 0, dpr, 0, 0)
    return ctx
  }, [width, height])
  return { ref, width, getCtx }
}

function cssVar(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim()
}

const OVERVIEW_H = 34
const DETAIL_H = 170
const BAND_H = 16 // 详细图顶部的幻灯片色带
const PAD_B = 16 // 底部时间刻度

export default function KeyframeTimeline(props: Props) {
  const { debug, slideIndex, duration, windowStart, span, currentTime, onWindowStart, onHover, onSeek } = props
  const overview = useCanvas(OVERVIEW_H)
  const detail = useCanvas(DETAIL_H)
  const [hoverX, setHoverX] = useState<number | null>(null)
  const dragging = useRef(false)

  const windowEnd = Math.min(duration, windowStart + span)
  const yMax = Math.max(debug.threshold * 6, 5)
  // 平方根刻度：小的变化也看得见，大的跳变不至于把图压扁
  const yOf = (v: number) => {
    const top = BAND_H + 6
    const h = DETAIL_H - top - PAD_B
    return top + h * (1 - Math.sqrt(Math.min(v, yMax) / yMax))
  }

  // ---------- 全片总览 ----------
  useEffect(() => {
    const ctx = overview.getCtx()
    const w = overview.width
    if (!ctx || !duration) return
    const x = (t: number) => (t / duration) * w
    ctx.clearRect(0, 0, w, OVERVIEW_H)
    ctx.fillStyle = cssVar('--surface-2')
    ctx.fillRect(0, 0, w, OVERVIEW_H)
    for (const r of debug.runs) {
      ctx.fillStyle = slideColor(slideIndex.get(r.slide) ?? 0, 0.85)
      ctx.fillRect(x(r.start), 4, Math.max(1, x(r.end) - x(r.start)), OVERVIEW_H - 14)
    }
    // 丢弃的空白段（0001 §3.5）：灰色，与幻灯片色块同一行
    ctx.fillStyle = cssVar('--text-2')
    ctx.globalAlpha = 0.35
    for (const [a, b] of debug.blank_runs ?? []) ctx.fillRect(x(a), 4, Math.max(1, x(b) - x(a)), OVERVIEW_H - 14)
    ctx.globalAlpha = 1
    ctx.fillStyle = cssVar('--danger')
    for (const [a, b] of debug.short_runs) ctx.fillRect(x(a), OVERVIEW_H - 8, Math.max(1, x(b) - x(a)), 6)
    // 当前窗口
    ctx.strokeStyle = cssVar('--text')
    ctx.lineWidth = 1.5
    ctx.strokeRect(x(windowStart) + 0.75, 1, Math.max(3, x(windowEnd) - x(windowStart)) - 1.5, OVERVIEW_H - 2)
    ctx.fillStyle = cssVar('--text')
    ctx.fillRect(x(currentTime), 0, 1.5, OVERVIEW_H)
  }, [overview, debug, slideIndex, duration, windowStart, windowEnd, currentTime])

  // ---------- 窗口内的帧差曲线 ----------
  useEffect(() => {
    const ctx = detail.getCtx()
    const w = detail.width
    if (!ctx || !duration) return
    const x = (t: number) => ((t - windowStart) / (windowEnd - windowStart)) * w
    const text2 = cssVar('--text-2')
    ctx.clearRect(0, 0, w, DETAIL_H)

    // 被丢弃的短暂段：浅红底
    ctx.fillStyle = cssVar('--danger-soft')
    for (const [a, b] of debug.short_runs) {
      if (b < windowStart || a > windowEnd) continue
      ctx.fillRect(x(a), BAND_H, Math.max(1, x(b) - x(a)), DETAIL_H - BAND_H - PAD_B)
    }

    // 丢弃的空白段：灰底，色带处标"空白"
    ctx.font = '11px ' + cssVar('--mono')
    ctx.textBaseline = 'middle'
    for (const [a, b] of debug.blank_runs ?? []) {
      if (b < windowStart || a > windowEnd) continue
      const x0 = Math.max(0, x(a))
      const x1 = Math.min(w, x(b))
      ctx.fillStyle = cssVar('--text-2')
      ctx.globalAlpha = 0.12
      ctx.fillRect(x0, BAND_H, Math.max(1, x1 - x0), DETAIL_H - BAND_H - PAD_B)
      ctx.globalAlpha = 0.45
      ctx.fillRect(x0, 0, Math.max(1, x1 - x0 - 1), BAND_H)
      ctx.globalAlpha = 1
      if (x1 - x0 > 30) {
        ctx.fillStyle = '#fff'
        ctx.fillText('空白', x0 + 3, BAND_H / 2)
      }
    }

    // 顶部幻灯片色带 + 标签 + 切分线
    ctx.font = '11px ' + cssVar('--mono')
    ctx.textBaseline = 'middle'
    for (const r of debug.runs) {
      if (r.end < windowStart || r.start > windowEnd) continue
      const x0 = Math.max(0, x(r.start))
      const x1 = Math.min(w, x(r.end))
      ctx.fillStyle = slideColor(slideIndex.get(r.slide) ?? 0, r.kind === 'new' ? 0.9 : 0.55)
      ctx.fillRect(x0, 0, Math.max(1, x1 - x0 - 1), BAND_H)
      if (x1 - x0 > 34) {
        ctx.fillStyle = '#fff'
        ctx.fillText(r.kind === 'revisit' ? `↺${r.slide}` : r.slide, x0 + 3, BAND_H / 2)
      }
      ctx.strokeStyle = slideColor(slideIndex.get(r.slide) ?? 0, 0.5)
      ctx.lineWidth = 1
      ctx.beginPath()
      ctx.moveTo(Math.round(x(r.start)) + 0.5, BAND_H)
      ctx.lineTo(Math.round(x(r.start)) + 0.5, DETAIL_H - PAD_B)
      ctx.stroke()
    }

    // 阈值线
    const hline = (v: number, color: string, dash: number[]) => {
      ctx.strokeStyle = color
      ctx.setLineDash(dash)
      ctx.beginPath()
      ctx.moveTo(0, yOf(v) + 0.5)
      ctx.lineTo(w, yOf(v) + 0.5)
      ctx.stroke()
      ctx.setLineDash([])
    }
    hline(debug.threshold, cssVar('--danger'), [4, 3])
    hline(debug.drift_threshold, text2, [2, 4])

    // 曲线：窗口内采样多于像素时按像素取最大值，保证尖峰不丢
    const pts = debug.trace
    let lo = 0
    let hi = pts.length
    while (lo < hi) {
      const mid = (lo + hi) >> 1
      if (pts[mid][0] < windowStart) lo = mid + 1
      else hi = mid
    }
    ctx.strokeStyle = cssVar('--accent')
    ctx.lineWidth = 1.2
    ctx.beginPath()
    let started = false
    let px = -1
    let pmax = 0
    const flush = () => {
      if (px < 0) return
      const y = yOf(pmax)
      if (!started) {
        ctx.moveTo(px, y)
        started = true
      } else ctx.lineTo(px, y)
    }
    for (let i = lo; i < pts.length && pts[i][0] <= windowEnd; i++) {
      const cx = Math.round(x(pts[i][0]))
      if (cx !== px) {
        flush()
        px = cx
        pmax = pts[i][1]
      } else pmax = Math.max(pmax, pts[i][1])
    }
    flush()
    ctx.stroke()

    // 时间刻度
    ctx.fillStyle = text2
    ctx.textBaseline = 'alphabetic'
    const steps = [5, 10, 30, 60, 120, 300, 600, 900, 1800, 3600]
    const step = steps.find((s) => (s / (windowEnd - windowStart)) * w >= 80) ?? 3600
    for (let t = Math.ceil(windowStart / step) * step; t <= windowEnd; t += step) {
      ctx.fillText(formatTime(t), x(t) + 2, DETAIL_H - 3)
      ctx.fillRect(x(t), DETAIL_H - PAD_B, 1, 4)
    }
    ctx.fillText(`阈值 ${debug.threshold}%`, 4, yOf(debug.threshold) - 3)

    // 当前播放位置与悬停线
    if (currentTime >= windowStart && currentTime <= windowEnd) {
      ctx.fillStyle = cssVar('--text')
      ctx.fillRect(x(currentTime), 0, 1.5, DETAIL_H - PAD_B)
    }
    if (hoverX != null) {
      ctx.fillStyle = text2
      ctx.fillRect(hoverX, BAND_H, 1, DETAIL_H - BAND_H - PAD_B)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [detail, debug, slideIndex, duration, windowStart, windowEnd, currentTime, hoverX])

  // ---------- 交互 ----------
  const tAt = (e: React.MouseEvent<HTMLCanvasElement>, from: number, to: number) => {
    const rect = e.currentTarget.getBoundingClientRect()
    const f = Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width))
    return { t: from + f * (to - from), x: e.clientX - rect.left }
  }
  const moveWindow = (e: React.MouseEvent<HTMLCanvasElement>) => {
    const { t } = tAt(e, 0, duration)
    onWindowStart(Math.min(Math.max(0, t - span / 2), Math.max(0, duration - span)))
  }
  const onWheel = (e: React.WheelEvent<HTMLCanvasElement>) => {
    const delta = (e.deltaY || e.deltaX) > 0 ? span / 4 : -span / 4
    onWindowStart(Math.min(Math.max(0, windowStart + delta), Math.max(0, duration - span)))
  }

  return (
    <div className="kf-timeline">
      <canvas
        ref={overview.ref}
        className="kf-overview"
        style={{ height: OVERVIEW_H }}
        onMouseDown={(e) => {
          dragging.current = true
          moveWindow(e)
        }}
        onMouseMove={(e) => dragging.current && moveWindow(e)}
        onMouseUp={() => (dragging.current = false)}
        onMouseLeave={() => (dragging.current = false)}
        title="点击或拖动选择下方放大显示的时间段"
      />
      <canvas
        ref={detail.ref}
        className="kf-detail"
        style={{ height: DETAIL_H }}
        onMouseMove={(e) => {
          const { t, x } = tAt(e, windowStart, windowEnd)
          setHoverX(x)
          onHover(t)
        }}
        onMouseLeave={() => {
          setHoverX(null)
          onHover(null)
        }}
        onClick={(e) => onSeek(tAt(e, windowStart, windowEnd).t)}
        onWheel={onWheel}
      />
    </div>
  )
}
