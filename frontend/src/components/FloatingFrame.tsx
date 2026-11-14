import { tr, useLocale } from '../i18n.ts'
import { useRef, useState, type CSSProperties, type KeyboardEvent, type PointerEvent, type ReactNode } from 'react'
import { clamp, type FloatBox } from '../useLayoutPrefs.ts'

interface Props {
  floating: boolean
  hidden?: boolean
  onHide: () => void
  box: FloatBox | null
  onBox: (box: FloatBox) => void
  /** 没有记录位置时，默认放在右下角并让开右侧这么宽（Agent 在右侧时不压住它的输入框） */
  avoidRight?: number
  /** 放回原位 */
  onDock: () => void
  children: ReactNode
}

const MIN_W = 280
const MARGIN = 16
/** 松手时离边缘不超过这个距离就贴过去（靠近角落时两边都贴，即吸附到角） */
const SNAP = 64
const KEY_STEP = 24
/** 拖动条 + 控制条的高度，加上 16:9 画面就是小窗高度 */
const CHROME_H = 24 + 40

const heightOf = (w: number) => Math.round((w * 9) / 16) + CHROME_H

/** 按当前窗口把小窗限制在可视区内；没有记录时放右下角 */
function fitBox(box: FloatBox | null, avoidRight = 0): FloatBox {
  const vw = window.innerWidth
  const vh = window.innerHeight
  const w = clamp(box?.w ?? 400, Math.min(MIN_W, vw - 16), Math.max(Math.min(MIN_W, vw - 16), Math.min(vw * 0.6, vw - 16)))
  const h = heightOf(w)
  const x = box ? box.x : vw - avoidRight - w - MARGIN
  const y = box ? box.y : vh - h - MARGIN
  return { x: clamp(x, 8, Math.max(8, vw - w - 8)), y: clamp(y, 8, Math.max(8, vh - h - 8)), w }
}

function snap(box: FloatBox): FloatBox {
  const vw = window.innerWidth
  const vh = window.innerHeight
  const h = heightOf(box.w)
  let { x, y } = box
  if (x < SNAP) x = MARGIN
  else if (vw - (x + box.w) < SNAP) x = vw - box.w - MARGIN
  if (y < SNAP) y = MARGIN
  else if (vh - (y + h) < SNAP) y = vh - h - MARGIN
  return fitBox({ ...box, x, y })
}

/** 视频容器：平时是网格里的一格；浮起时变成可拖动、可缩放的页面内小窗。
 *  两种状态渲染同一结构，里面的播放器不会重新挂载，播放不中断。 */
export default function FloatingFrame({ floating, hidden, onHide, box, avoidRight = 0, onBox, onDock, children }: Props) {
  useLocale()
  const [live, setLiveState] = useState<FloatBox | null>(null)
  const liveRef = useRef<FloatBox | null>(null)
  const setLive = (b: FloatBox | null) => { liveRef.current = b; setLiveState(b) }
  const drag = useRef<{ kind: 'move' | 'resize'; px: number; py: number; base: FloatBox } | null>(null)
  const shown = live ?? fitBox(box, avoidRight)

  const start = (kind: 'move' | 'resize') => (e: PointerEvent<HTMLDivElement>) => {
    if (e.button !== 0 || (e.target as HTMLElement).closest('button')) return
    e.preventDefault()
    e.currentTarget.setPointerCapture(e.pointerId)
    drag.current = { kind, px: e.clientX, py: e.clientY, base: shown }
  }
  const move = (e: PointerEvent<HTMLDivElement>) => {
    const d = drag.current
    if (!d) return
    if (!(e.buttons & 1)) { end(); return }
    const dx = e.clientX - d.px
    const dy = e.clientY - d.py
    setLive(fitBox(d.kind === 'move' ? { ...d.base, x: d.base.x + dx, y: d.base.y + dy } : { ...d.base, w: d.base.w + dx }))
  }
  const end = () => {
    const d = drag.current
    drag.current = null
    const cur = liveRef.current
    if (!d || !cur) return
    onBox(d.kind === 'move' ? snap(cur) : cur)
    setLive(null)
  }
  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.target !== e.currentTarget) return
    const step = e.shiftKey ? KEY_STEP * 4 : KEY_STEP
    const dx = e.key === 'ArrowLeft' ? -step : e.key === 'ArrowRight' ? step : 0
    const dy = e.key === 'ArrowUp' ? -step : e.key === 'ArrowDown' ? step : 0
    if (!dx && !dy) return
    e.preventDefault()
    onBox(fitBox({ ...shown, x: shown.x + dx, y: shown.y + dy }))
  }

  const style = floating ? ({ left: shown.x, top: shown.y, width: shown.w } as CSSProperties) : undefined
  return (
    <div id="vp-player" hidden={hidden} className={`vp-player${floating ? ' is-floating' : ''}${live ? ' is-dragging' : ''}`} style={style}>
      {floating && (
        <div className="float-bar" role="group" tabIndex={0} aria-label={tr("视频小窗：拖动或用方向键移动，双击放回")}
          onPointerDown={start('move')} onPointerMove={move} onPointerUp={end} onPointerCancel={end} onLostPointerCapture={end}
          onDoubleClick={onDock} onKeyDown={onKey}>
          <svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor" aria-hidden>
            {[4, 8, 12].flatMap((y) => [6, 10].map((x) => <circle key={`${x}-${y}`} cx={x} cy={y} r="1.1" />))}
          </svg>
          <span className="float-bar-hint">{tr("拖动移动")}</span>
          <span className="spacer" />
          <button type="button" className="float-dock" onClick={onHide}>{tr("收起视频")}</button>
          <button type="button" className="float-dock" onClick={onDock} title={tr("放回原位")}>
            <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" aria-hidden><rect x="1.75" y="2.75" width="12.5" height="10.5" rx="2" /><path d="M5 6l3 3 3-3" /></svg>{tr("放回")}</button>
        </div>
      )}
      {children}
      {floating && (
        <div className="float-resize" aria-hidden title={tr("拖动调整小窗大小")}
          onPointerDown={start('resize')} onPointerMove={move} onPointerUp={end} onPointerCancel={end} onLostPointerCapture={end} />
      )}
    </div>
  )
}
