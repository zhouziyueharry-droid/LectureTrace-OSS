import { useEffect, useRef, useState, type ReactNode } from 'react'
import { errorText, formatTime, frameUrl, saveKeyframeRoi, type Roi, type VideoDetail } from '../api.ts'
import Modal from './Modal.tsx'

interface Props {
  videoId: string
  /** modal：弹窗（抽帧展示台、修改区域）；card：嵌在视频页里的卡片（新视频确认区域） */
  variant?: 'modal' | 'card'
  /** 仅 modal 使用 */
  open?: boolean
  /** 初始截图时间点（通常是播放器当前位置） */
  time: number
  /** 视频时长，已知时显示时间滑块，可换一个时间点截图 */
  duration?: number | null
  /** 预填的框；null 表示不预填（整个画面） */
  initial: Roi | null
  /** 主按钮文字 */
  confirmLabel?: string
  /** 标题下方的补充说明 */
  intro?: ReactNode
  onClose?: () => void
  onSaved: (video: VideoDetail) => void
}

const clamp01 = (v: number) => Math.min(1, Math.max(0, v))
const STEP_S = 60

/** 在视频截图上框选幻灯片区域并确认（提案 0001 §2.5）。确认后后端开始抽帧，已抽过帧时按新区域重新抽帧。 */
export default function RoiEditor({
  videoId,
  variant = 'modal',
  open = true,
  time,
  duration,
  initial,
  confirmLabel = '确认',
  intro,
  onClose,
  onSaved,
}: Props) {
  const [roi, setRoi] = useState<Roi | null>(initial)
  const [asDefault, setAsDefault] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // t：滑块显示的时间；shotT：实际截图的时间（拖动停下 300ms 后再请求，避免连续请求截图）
  const [t, setT] = useState(() => Math.max(0, time))
  const [shotT, setShotT] = useState(() => Math.max(0, time))
  // 按时间点记录截图是否加载完成，换时间点后自动回到"加载中"
  const [loaded, setLoaded] = useState<{ t: number; ok: boolean } | null>(null)
  const imgState = loaded?.t !== shotT ? 'loading' : loaded.ok ? 'ok' : 'error'
  const start = useRef<[number, number] | null>(null)
  const boxRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const timer = setTimeout(() => setShotT(t), 300)
    return () => clearTimeout(timer)
  }, [t])

  const maxT = duration && duration > 0 ? Math.max(0, duration - 1) : null
  const moveT = (delta: number) => setT((cur) => Math.max(0, Math.min(maxT ?? Infinity, Math.round(cur + delta))))

  const pos = (e: React.PointerEvent): [number, number] => {
    const r = boxRef.current!.getBoundingClientRect()
    return [clamp01((e.clientX - r.left) / r.width), clamp01((e.clientY - r.top) / r.height)]
  }

  const onDown = (e: React.PointerEvent) => {
    e.currentTarget.setPointerCapture(e.pointerId)
    start.current = pos(e)
    setRoi(null)
  }
  const onMove = (e: React.PointerEvent) => {
    if (!start.current) return
    const [x0, y0] = start.current
    const [x1, y1] = pos(e)
    setRoi([Math.min(x0, x1), Math.min(y0, y1), Math.abs(x1 - x0), Math.abs(y1 - y0)])
  }
  const onUp = () => {
    start.current = null
  }

  const tooSmall = roi != null && (roi[2] < 0.05 || roi[3] < 0.05)

  const save = async (fullFrame: boolean) => {
    setBusy(true)
    setError(null)
    try {
      const v = await saveKeyframeRoi(videoId, fullFrame ? null : roi, asDefault, { fullFrame })
      onSaved(v)
    } catch (e) {
      setError(errorText(e))
    } finally {
      setBusy(false)
    }
  }

  const actions = (
    <>
      <label className="check">
        <input type="checkbox" checked={asDefault} onChange={(e) => setAsDefault(e.target.checked)} />
        设为以后导入视频的默认区域
      </label>
      <span className="spacer" />
      <button className="btn" onClick={() => void save(true)} disabled={busy}>
        使用整个画面
      </button>
      <button className="btn btn-primary" onClick={() => void save(false)} disabled={busy || !roi || tooSmall}>
        {busy ? '保存中…' : confirmLabel}
      </button>
    </>
  )

  const body = (
    <>
      {intro}
      <p className="muted small">
        在截图上拖出幻灯片所在的矩形。抽帧时只比较框内的画面，保存的截图也只保留框内部分，可排除摄像头画面的干扰，并减少以后发给模型的图片大小。
      </p>
      <div className="roi-time">
        <button className="btn btn-small" onClick={() => moveT(-STEP_S)} disabled={t <= 0} title="往前 1 分钟">
          ‹ 1 分钟
        </button>
        {maxT != null && (
          <input
            type="range"
            min={0}
            max={maxT}
            step={1}
            value={Math.min(t, maxT)}
            onChange={(e) => setT(Number(e.target.value))}
            aria-label="截图时间点"
          />
        )}
        <button
          className="btn btn-small"
          onClick={() => moveT(STEP_S)}
          disabled={maxT != null && t >= maxT}
          title="往后 1 分钟"
        >
          1 分钟 ›
        </button>
        <span className="small mono muted">截图时间 {formatTime(t)}</span>
      </div>
      <div
        ref={boxRef}
        className="roi-box"
        onPointerDown={onDown}
        onPointerMove={onMove}
        onPointerUp={onUp}
        onPointerCancel={onUp}
      >
        <img
          src={frameUrl(videoId, shotT, 1280)}
          alt="视频截图"
          draggable={false}
          onLoad={() => setLoaded({ t: shotT, ok: true })}
          onError={() => setLoaded({ t: shotT, ok: false })}
        />
        {imgState !== 'ok' && (
          <div className="roi-box-msg small">
            {imgState === 'loading' ? '正在截取画面…' : '这个时间点没有画面，请换一个时间点'}
          </div>
        )}
        {roi && (
          <div
            className={`roi-rect${tooSmall ? ' roi-rect-bad' : ''}`}
            style={{
              left: `${roi[0] * 100}%`,
              top: `${roi[1] * 100}%`,
              width: `${roi[2] * 100}%`,
              height: `${roi[3] * 100}%`,
            }}
          />
        )}
      </div>
      <p className="small mono muted">
        {roi
          ? `x ${(roi[0] * 100).toFixed(1)}%  y ${(roi[1] * 100).toFixed(1)}%  宽 ${(roi[2] * 100).toFixed(1)}%  高 ${(roi[3] * 100).toFixed(1)}%`
          : '尚未框选'}
        {tooSmall && <span className="error-text">（太小：宽高都要不小于画面的 5%）</span>}
      </p>
      {error && <p className="error-text">{error}</p>}
    </>
  )

  if (variant === 'card') {
    return (
      <section className="panel roi-card" aria-label="框选幻灯片区域">
        <div className="panel-head">
          <h2>框选幻灯片区域</h2>
          {onClose && (
            <button className="btn btn-small" onClick={onClose} disabled={busy}>
              稍后再说
            </button>
          )}
        </div>
        <div className="panel-body">
          {body}
          <div className="roi-card-actions">{actions}</div>
        </div>
      </section>
    )
  }

  return (
    <Modal open={open} title="框选幻灯片区域" onClose={() => !busy && onClose?.()} width={900} footer={actions}>
      {body}
    </Modal>
  )
}
