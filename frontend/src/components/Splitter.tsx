import { useRef, type KeyboardEvent, type PointerEvent } from 'react'

interface Props {
  /** 'col' 是竖条（调宽度），'row' 是横条（调高度） */
  direction: 'col' | 'row'
  label: string
  /** 被调整的区域，以及以像素计的当前尺寸和范围 */
  controls: string
  value: number
  min: number
  max: number
  /** 按下时调用，返回当前尺寸作为拖拽基准 */
  onStart: () => number
  /** 拖拽中：基准尺寸加上指针位移后的新尺寸（未限幅，由调用方限幅） */
  onResize: (size: number) => void
  /** 位移方向：1 表示向右/向下变大，-1 表示向左/向上变大 */
  sign?: 1 | -1
  /** 双击恢复默认 */
  onReset?: () => void
  className?: string
}

const KEY_STEP = 24

/** 可拖拽也可用方向键调整的分隔条，鼠标、触屏、键盘共用一套 */
export default function Splitter({ direction, label, controls, value, min, max, onStart, onResize, sign = 1, onReset, className }: Props) {
  const drag = useRef<{ p: number; base: number } | null>(null)
  const col = direction === 'col'

  const onPointerDown = (e: PointerEvent<HTMLDivElement>) => {
    if (e.button !== 0) return
    e.currentTarget.setPointerCapture(e.pointerId)
    drag.current = { p: col ? e.clientX : e.clientY, base: onStart() }
  }
  const onPointerMove = (e: PointerEvent<HTMLDivElement>) => {
    const d = drag.current
    if (!d) return
    onResize(d.base + sign * ((col ? e.clientX : e.clientY) - d.p))
  }
  const end = () => {
    drag.current = null
  }
  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    const grow = col ? (sign === 1 ? 'ArrowRight' : 'ArrowLeft') : sign === 1 ? 'ArrowDown' : 'ArrowUp'
    const shrink = col ? (sign === 1 ? 'ArrowLeft' : 'ArrowRight') : sign === 1 ? 'ArrowUp' : 'ArrowDown'
    if (e.key !== grow && e.key !== shrink) return
    e.preventDefault()
    onResize(onStart() + (e.key === grow ? KEY_STEP : -KEY_STEP))
  }

  return (
    <div
      role="separator"
      aria-orientation={col ? 'vertical' : 'horizontal'}
      aria-label={label}
      aria-controls={controls}
      aria-valuenow={Math.round(value)}
      aria-valuemin={Math.floor(min)}
      aria-valuemax={Math.ceil(max)}
      aria-valuetext={`${Math.round(value)} 像素`}
      title={onReset ? `${label}（双击恢复）` : label}
      tabIndex={0}
      className={`splitter splitter-${direction}${className ? ` ${className}` : ''}`}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={end}
      onPointerCancel={end}
      onLostPointerCapture={end}
      onDoubleClick={onReset}
      onKeyDown={onKeyDown}
    >
      <span className="splitter-grip" />
    </div>
  )
}
