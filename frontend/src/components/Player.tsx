import { useEffect, useId, useRef, useState, type CSSProperties, type KeyboardEvent as ReactKeyboardEvent, type PointerEvent, type RefObject } from 'react'
import { formatTime, subtitleUrl, videoFileUrl } from '../api.ts'
import { clamp, VIDEO_H_MIN } from '../useLayoutPrefs.ts'
import { imageBounds, validViewCrop, viewSelection, viewTransform, type ViewCrop } from './playerCrop.ts'
import Splitter from './Splitter.tsx'

export type SubMode = 'bilingual' | 'source' | 'off'

const MODE_KEY = 'lt.subMode.v2'
const SUBTITLE_SIZE_KEY = 'lt.subtitleSize.v1'
const SUBTITLE_SIZES = [40, 50, 60, 75, 100, 125, 150, 200, 250] as const
const cropKey = (videoId: string) => `lt.viewCrop.v1.${videoId}`

function loadMode(): SubMode {
  try {
    const v = localStorage.getItem(MODE_KEY)
    if (v === 'bilingual' || v === 'source' || v === 'off') return v
  } catch { /* 存储不可用 */ }
  return 'bilingual'
}

function loadSubtitleSize(): number {
  try {
    const value = Number(localStorage.getItem(SUBTITLE_SIZE_KEY))
    if (SUBTITLE_SIZES.some((size) => size === value)) return value
  } catch { /* 存储不可用 */ }
  return 100
}

function loadCrop(videoId: string): ViewCrop | null {
  try {
    const raw = localStorage.getItem(cropKey(videoId))
    if (raw) {
      const value: unknown = JSON.parse(raw)
      if (validViewCrop(value)) return value
    }
  } catch { /* 存储不可用或旧数据无效 */ }
  return null
}

interface Props {
  videoId: string
  videoRef: RefObject<HTMLVideoElement>
  subsVersion: string | null
  subsHint: string
  onTime: (t: number) => void
  videoH: number | null
  /** 拖动视频高度的上限；不传时按窗口高度 */
  videoHMax?: number
  /** 视频下方分隔条的说明；Agent 停在视频下方时它同时调整 Agent 的高度 */
  splitLabel?: string
  onVideoH: (h: number | null) => void
  /** 视频是否浮成页面内小窗 */
  floating?: boolean
  onToggleFloat?: () => void
  onHide?: () => void
}

const RATES = [0.5, 0.75, 1, 1.25, 1.5, 2]

const MODES: { value: SubMode; label: string }[] = [
  { value: 'bilingual', label: '双语' },
  { value: 'source', label: '原文' },
  { value: 'off', label: '关闭' },
]

function ControlIcon({ name }: { name: 'play' | 'pause' | 'volume' | 'muted' | 'crop' | 'expand' | 'collapse' | 'captions' | 'float' | 'dock' }) {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      {name === 'play' && <path d="m8 5 11 7-11 7z" />}
      {name === 'pause' && <><path d="M8 5v14" /><path d="M16 5v14" /></>}
      {(name === 'volume' || name === 'muted') && <><path d="M4 9v6h4l5 4V5L8 9z" />{name === 'volume' ? <><path d="M17 9a5 5 0 0 1 0 6" /><path d="M20 6a9 9 0 0 1 0 12" /></> : <><path d="m17 9 5 6" /><path d="m22 9-5 6" /></>}</>}
      {name === 'crop' && <><path d="M4 9V4h5M15 4h5v5M20 15v5h-5M9 20H4v-5" /><rect x="7" y="7" width="10" height="10" rx="1" /></>}
      {name === 'expand' && <><path d="M9 4H4v5M15 4h5v5M20 15v5h-5M9 20H4v-5" /></>}
      {name === 'collapse' && <><path d="M4 9h5V4M20 9h-5V4M20 15h-5v5M4 15h5v5" /></>}
      {name === 'captions' && <><rect x="3" y="5" width="18" height="14" rx="2" /><path d="M7 15h4M13 15h4M7 11h10" /></>}
      {name === 'float' && <><rect x="3" y="5" width="18" height="14" rx="2" /><rect x="12" y="12" width="7" height="5" rx="1" /></>}
      {name === 'dock' && <><rect x="3" y="5" width="18" height="14" rx="2" /><path d="M9 10l3 3 3-3" /></>}
    </svg>
  )
}

export default function Player({ videoId, videoRef, subsVersion, subsHint, onTime, videoH, videoHMax, splitLabel, onVideoH, floating = false, onToggleFloat, onHide }: Props) {
  const screenId = useId()
  const wrapRef = useRef<HTMLDivElement>(null)
  const screenRef = useRef<HTMLDivElement>(null)
  const cropLayerRef = useRef<HTMLDivElement>(null)
  const volumeRef = useRef<HTMLDivElement>(null)
  const volumeButtonRef = useRef<HTMLButtonElement>(null)
  const settingsRef = useRef<HTMLDivElement>(null)
  const settingsButtonRef = useRef<HTMLButtonElement>(null)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const dragStart = useRef<[number, number] | null>(null)
  const bilRef = useRef<HTMLTrackElement>(null)
  const srcRef = useRef<HTMLTrackElement>(null)
  const [mode, setMode] = useState<SubMode>(loadMode)
  const [subtitleSize, setSubtitleSize] = useState(loadSubtitleSize)
  const [crop, setCrop] = useState<ViewCrop | null>(() => loadCrop(videoId))
  const [selecting, setSelecting] = useState(false)
  const [selection, setSelection] = useState<ViewCrop | null>(null)
  const [size, setSize] = useState({ width: 0, height: 0 })
  const [source, setSource] = useState({ width: 0, height: 0 })
  const [playing, setPlaying] = useState(false)
  const [current, setCurrent] = useState(0)
  const [duration, setDuration] = useState(0)
  const [volume, setVolume] = useState(1)
  const [muted, setMuted] = useState(false)
  const [volumeOpen, setVolumeOpen] = useState(false)
  const [rate, setRate] = useState(1)
  const [fullScreen, setFullScreen] = useState(false)
  const [captions, setCaptions] = useState<string[]>([])
  const [trackLoaded, setTrackLoaded] = useState(0)
  const [trackError, setTrackError] = useState(false)
  const [videoError, setVideoError] = useState(false)
  const ready = subsVersion !== null
  const canSelect = source.width > 0 && source.height > 0 && size.width > 0 && size.height > 0

  useEffect(() => {
    try { localStorage.setItem(MODE_KEY, mode) } catch { /* 存储不可用 */ }
  }, [mode])

  useEffect(() => {
    try { localStorage.setItem(SUBTITLE_SIZE_KEY, String(subtitleSize)) } catch { /* 存储不可用 */ }
  }, [subtitleSize])

  useEffect(() => { setTrackError(false) }, [subsVersion])

  useEffect(() => {
    if (!volumeOpen) return
    const dismiss = (event: globalThis.PointerEvent) => {
      if (event.target instanceof Node && !volumeRef.current?.contains(event.target)) setVolumeOpen(false)
    }
    document.addEventListener('pointerdown', dismiss)
    return () => document.removeEventListener('pointerdown', dismiss)
  }, [volumeOpen])

  useEffect(() => {
    if (!settingsOpen) return
    const dismiss = (event: globalThis.PointerEvent) => {
      if (event.target instanceof Node && !settingsRef.current?.contains(event.target)) setSettingsOpen(false)
    }
    document.addEventListener('pointerdown', dismiss)
    return () => document.removeEventListener('pointerdown', dismiss)
  }, [settingsOpen])

  useEffect(() => {
    const screen = screenRef.current
    if (!screen) return
    const observer = new ResizeObserver(() => setSize({ width: screen.clientWidth, height: screen.clientHeight }))
    observer.observe(screen)
    return () => observer.disconnect()
  }, [])

  useEffect(() => {
    const onChange = () => setFullScreen(document.fullscreenElement === wrapRef.current)
    document.addEventListener('fullscreenchange', onChange)
    return () => document.removeEventListener('fullscreenchange', onChange)
  }, [])

  useEffect(() => {
    if (!selecting) return
    cropLayerRef.current?.focus()
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { setSelecting(false); setSelection(null) }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [selecting])

  // Hidden tracks keep activeCues available without painting captions on the scaled video.
  useEffect(() => {
    const bilingual = bilRef.current?.track
    const original = srcRef.current?.track
    if (!ready) { setCaptions([]); return }
    if (bilingual) bilingual.mode = mode === 'bilingual' ? 'hidden' : 'disabled'
    if (original) original.mode = mode === 'source' ? 'hidden' : 'disabled'
    const active = mode === 'bilingual' ? bilingual : mode === 'source' ? original : null
    const update = () => setCaptions(Array.from(active?.activeCues ?? [], (cue) => (cue as VTTCue).getCueAsHTML().textContent ?? ''))
    update()
    active?.addEventListener('cuechange', update)
    return () => active?.removeEventListener('cuechange', update)
  }, [mode, ready, subsVersion, trackLoaded])

  const image = canSelect ? imageBounds(size.width, size.height, source.width, source.height) : null
  const activeCrop = selecting ? null : crop
  const cropStyle = activeCrop && canSelect ? viewTransform(activeCrop, size.width, size.height, source.width, source.height) : undefined
  const selectionStyle = selection && image ? {
    left: image.x + selection[0] * image.width,
    top: image.y + selection[1] * image.height,
    width: selection[2] * image.width,
    height: selection[3] * image.height,
  } : undefined

  const position = (event: PointerEvent<HTMLDivElement>): [number, number] => {
    const rect = screenRef.current!.getBoundingClientRect()
    const bounds = imageBounds(rect.width, rect.height, source.width, source.height)
    return [
      clamp((event.clientX - rect.left - bounds.x) / bounds.width, 0, 1),
      clamp((event.clientY - rect.top - bounds.y) / bounds.height, 0, 1),
    ]
  }

  const startSelection = (event: PointerEvent<HTMLDivElement>) => {
    if (!selecting || !canSelect) return
    event.currentTarget.setPointerCapture(event.pointerId)
    dragStart.current = position(event)
    setSelection(null)
  }
  const moveSelection = (event: PointerEvent<HTMLDivElement>) => {
    if (!dragStart.current || !canSelect) return
    setSelection(viewSelection(dragStart.current, position(event), size.width / size.height, source.width / source.height))
  }
  const stopSelection = () => { dragStart.current = null }

  const confirmSelection = () => {
    if (!validViewCrop(selection)) return
    setCrop(selection)
    setSelecting(false)
    setSelection(null)
    try { localStorage.setItem(cropKey(videoId), JSON.stringify(selection)) } catch { /* 当前页面仍可使用 */ }
  }
  const adjustSelection = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (event.target !== event.currentTarget || !canSelect) return
    if (event.key === 'Enter') { event.preventDefault(); confirmSelection(); return }
    if (event.key === 'Escape') { event.preventDefault(); setSelecting(false); setSelection(null); return }
    if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', '+', '=', '-'].includes(event.key)) return
    event.preventDefault()
    setSelection((old) => {
      const [x, y, w, h] = old ?? viewSelection([0.25, 0.25], [0.75, 0.75], size.width / size.height, source.width / source.height)
      if (event.key.startsWith('Arrow')) {
        const step = event.shiftKey ? 0.08 : 0.02
        return [clamp(x + (event.key === 'ArrowRight' ? step : event.key === 'ArrowLeft' ? -step : 0), 0, 1 - w), clamp(y + (event.key === 'ArrowDown' ? step : event.key === 'ArrowUp' ? -step : 0), 0, 1 - h), w, h]
      }
      const factor = clamp(event.key === '-' ? 0.9 : 1.1, Math.max(0.1 / w, 0.1 / h), Math.min(1 / w, 1 / h))
      const nextW = w * factor
      const nextH = h * factor
      return [clamp(x + (w - nextW) / 2, 0, 1 - nextW), clamp(y + (h - nextH) / 2, 0, 1 - nextH), nextW, nextH]
    })
  }
  const restore = () => {
    setCrop(null)
    setSelecting(false)
    setSelection(null)
    try { localStorage.removeItem(cropKey(videoId)) } catch { /* 当前页面仍可使用 */ }
  }
  const changeRate = (value: number) => {
    if (videoRef.current) videoRef.current.playbackRate = value
  }
  const startCrop = () => {
    videoRef.current?.pause()
    setSelection(null)
    setSelecting(true)
  }
  const togglePlay = () => {
    const video = videoRef.current
    if (!video) return
    if (video.paused) void video.play().catch(() => { /* 浏览器禁止播放 */ })
    else video.pause()
  }
  const seekWithKeyboard = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (selecting || event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return
    const target = event.target
    if (!(target instanceof HTMLElement) || target.isContentEditable) return
    if (target.closest('select, textarea, .video-volume-control, input:not(.video-seek)')) return
    const video = videoRef.current
    if (!video || !Number.isFinite(video.duration) || video.duration <= 0) return
    event.preventDefault()
    video.currentTime = clamp(video.currentTime + (event.key === 'ArrowLeft' ? -10 : 10), 0, video.duration)
  }
  const toggleFullScreen = () => {
    if (document.fullscreenElement) void document.exitFullscreen().catch(() => { /* 浏览器拒绝全屏 */ })
    else void wrapRef.current?.requestFullscreen().catch(() => { /* 浏览器拒绝全屏 */ })
  }

  return (
    <section className="player" aria-label="视频播放器">
      <div ref={wrapRef} tabIndex={0} role="group" aria-label="播放器：左方向键后退 10 秒，右方向键前进 10 秒" onKeyDown={seekWithKeyboard} className={`video-wrap${videoH != null ? ' has-h' : ''}`} style={videoH != null ? ({ '--video-h': `${videoH}px` } as CSSProperties) : undefined}>
        <div id={screenId} ref={screenRef} className={`video-screen${selecting ? ' is-selecting' : ''}`} onPointerDown={(event) => { if (!selecting) wrapRef.current?.focus({ preventScroll: true }); startSelection(event) }} onPointerMove={moveSelection} onPointerUp={stopSelection} onPointerCancel={stopSelection}>
          <video
            ref={videoRef}
            src={videoFileUrl(videoId)}
            preload="metadata"
            playsInline
            style={cropStyle}
            onLoadedMetadata={(e) => {
              const video = e.currentTarget
              setSource({ width: video.videoWidth, height: video.videoHeight })
              setDuration(Number.isFinite(video.duration) ? video.duration : 0)
            }}
            onDurationChange={(e) => setDuration(Number.isFinite(e.currentTarget.duration) ? e.currentTarget.duration : 0)}
            onTimeUpdate={(e) => { setCurrent(e.currentTarget.currentTime); onTime(e.currentTarget.currentTime) }}
            onSeeked={(e) => { setCurrent(e.currentTarget.currentTime); onTime(e.currentTarget.currentTime) }}
            onPlay={() => setPlaying(true)}
            onPause={() => setPlaying(false)}
            onVolumeChange={(e) => { setVolume(e.currentTarget.volume); setMuted(e.currentTarget.muted) }}
            onRateChange={(e) => setRate(e.currentTarget.playbackRate)}
            onError={() => setVideoError(true)}
            onLoadedData={() => setVideoError(false)}
          >
            {ready && <track key={`bil-${subsVersion}`} ref={bilRef} kind="subtitles" label="双语" srcLang="zh" src={subtitleUrl(videoId, 'bilingual', subsVersion)} onLoad={() => setTrackLoaded((n) => n + 1)} onError={() => setTrackError(true)} />}
            {ready && <track key={`src-${subsVersion}`} ref={srcRef} kind="subtitles" label="原文" srcLang="en" src={subtitleUrl(videoId, 'source', subsVersion)} onLoad={() => setTrackLoaded((n) => n + 1)} onError={() => setTrackError(true)} />}
          </video>
          {captions.length > 0 && <div className="video-captions" aria-live="off" style={{ fontSize: `calc(clamp(12px, 1.9cqw, 26px) * ${subtitleSize / 100})` }}>{captions.map((line, i) => <span key={i}>{line}</span>)}</div>}
          {selecting && (
            <div ref={cropLayerRef} className="view-crop-layer" role="group" tabIndex={0} aria-label="框选观看区域：方向键移动，加减键缩放，回车确认" onKeyDown={adjustSelection}>
              {selectionStyle && <div className="view-crop-rect" style={selectionStyle} />}
              <div className="view-crop-actions" onPointerDown={(event) => event.stopPropagation()}>
                <span>拖动框选要放大的画面；也可用方向键移动、加减键缩放</span>
                <button className="btn btn-small btn-primary" disabled={!validViewCrop(selection)} onClick={confirmSelection}>确认</button>
                <button className="btn btn-small" onClick={() => { setSelecting(false); setSelection(null) }}>取消</button>
              </div>
            </div>
          )}
        </div>
        <div className="video-controls" role="group" aria-label="视频控制">
          <button type="button" aria-label={playing ? '暂停' : '播放'} title={playing ? '暂停' : '播放'} onClick={togglePlay}><ControlIcon name={playing ? 'pause' : 'play'} /></button>
          <span className="video-time mono">{formatTime(current)} / {formatTime(duration)}</span>
          <input className="video-seek" type="range" min={0} max={duration || 0} step={0.1} value={Math.min(current, duration || 0)} aria-label="播放进度" onChange={(e) => { if (videoRef.current) videoRef.current.currentTime = Number(e.target.value) }} />
          <div ref={volumeRef} className="video-volume-control" onBlur={(event) => {
            if (!event.currentTarget.contains(event.relatedTarget)) setVolumeOpen(false)
          }} onKeyDown={(event) => {
            if (event.key === 'Escape' && volumeOpen) {
              event.preventDefault()
              event.stopPropagation()
              setVolumeOpen(false)
              volumeButtonRef.current?.focus()
            }
          }}>
            <button ref={volumeButtonRef} type="button" aria-label="音量设置" title="音量设置" aria-expanded={volumeOpen} onClick={() => setVolumeOpen((open) => !open)}><ControlIcon name={muted || volume === 0 ? 'muted' : 'volume'} /></button>
            {volumeOpen && <div className="video-volume-popover" role="group" aria-label="音量设置面板">
              <span className="video-volume-value mono">{Math.round((muted ? 0 : volume) * 100)}%</span>
              <input className="video-volume" type="range" min={0} max={1} step={0.01} value={muted ? 0 : volume} aria-label="音量" aria-orientation="vertical" onChange={(e) => {
                const video = videoRef.current
                if (video) { video.volume = Number(e.target.value); video.muted = false }
              }} />
              <button type="button" aria-label={muted || volume === 0 ? '取消静音' : '静音'} title={muted || volume === 0 ? '取消静音' : '静音'} onClick={() => {
                const video = videoRef.current
                if (!video) return
                if (video.muted || video.volume === 0) {
                  if (video.volume === 0) video.volume = 1
                  video.muted = false
                } else video.muted = true
              }}><ControlIcon name={muted || volume === 0 ? 'muted' : 'volume'} /></button>
            </div>}
          </div>
          {/* 播放器较宽时框选和倍速直接放在控制条上；变窄后收进「字幕」弹层（见 styles.css 的 @container player） */}
          <button type="button" className="vc-wide" aria-label="框选画面" title="框选画面" disabled={!canSelect} aria-pressed={selecting} onClick={startCrop}><ControlIcon name="crop" /></button>
          {crop && <button type="button" className="vc-wide" aria-label="还原原画面" title="还原原画面" onClick={restore}>还原</button>}
          <select className="vc-wide" aria-label="播放速度" title="播放速度" value={rate} onChange={(e) => changeRate(Number(e.target.value))}>
            {RATES.map((value) => <option key={value} value={value}>{value}×</option>)}
          </select>
          <div ref={settingsRef} className="video-settings" onBlur={(event) => {
            if (!event.currentTarget.contains(event.relatedTarget)) setSettingsOpen(false)
          }} onKeyDown={(event) => {
            if (event.key === 'Escape' && settingsOpen) {
              event.preventDefault()
              event.stopPropagation()
              setSettingsOpen(false)
              settingsButtonRef.current?.focus()
            }
          }}>
            <button ref={settingsButtonRef} type="button" className="video-settings-btn" aria-label="字幕与播放设置" title={ready ? '字幕与播放设置' : subsHint} aria-expanded={settingsOpen} onClick={() => setSettingsOpen((open) => !open)}>
              <ControlIcon name="captions" />
              <span className="vc-wide-text">{ready ? MODES.find((item) => item.value === mode)?.label : '字幕'}</span>
            </button>
            {settingsOpen && <div className="video-settings-popover" role="group" aria-label="字幕与播放设置">
              <div className="vs-row">
                <span className="vs-label">字幕</span>
                {ready ? (
                  <div className="vs-seg" role="radiogroup" aria-label="字幕显示方式">
                    {MODES.map((item) => <button key={item.value} type="button" role="radio" aria-checked={mode === item.value} className={mode === item.value ? 'is-on' : ''} onClick={() => setMode(item.value)}>{item.label}</button>)}
                  </div>
                ) : <span className="vs-note">{subsHint}</span>}
              </div>
              <label className="vs-row">
                <span className="vs-label">字号</span>
                <select value={subtitleSize} disabled={!ready} onChange={(e) => setSubtitleSize(Number(e.target.value))}>
                  {SUBTITLE_SIZES.map((size) => <option key={size} value={size}>{size}%</option>)}
                </select>
              </label>
              <label className="vs-row vc-narrow">
                <span className="vs-label">倍速</span>
                <select value={rate} onChange={(e) => changeRate(Number(e.target.value))}>
                  {RATES.map((value) => <option key={value} value={value}>{value}×</option>)}
                </select>
              </label>
              <div className="vs-row vc-narrow">
                <span className="vs-label">画面</span>
                <button type="button" className="vs-action" disabled={!canSelect} onClick={() => { setSettingsOpen(false); startCrop() }}>框选放大</button>
                {crop && <button type="button" className="vs-action" onClick={() => { setSettingsOpen(false); restore() }}>还原</button>}
              </div>
            </div>}
          </div>
          {onToggleFloat && <button type="button" aria-label="小窗模式" title={floating ? '放回原位' : '浮成小窗：空出这一列给大纲和笔记'} aria-pressed={floating} onClick={onToggleFloat}><ControlIcon name={floating ? 'dock' : 'float'} /></button>}
          {!floating && onHide && <button type="button" title="收起视频" aria-label="收起视频" onClick={onHide}><svg width="18" height="18" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden><path d="M4 10h12" /></svg></button>}
          <button type="button" aria-label={fullScreen ? '退出全屏' : '全屏'} title={fullScreen ? '退出全屏' : '全屏'} onClick={toggleFullScreen}><ControlIcon name={fullScreen ? 'collapse' : 'expand'} /></button>
        </div>
      </div>
      <Splitter className="player-split" direction="row" label={splitLabel ?? '拖拽调整视频高度'} controls={screenId} value={size.height} min={Math.min(VIDEO_H_MIN, size.height)} max={Math.max(size.height, videoHMax ?? window.innerHeight - 160)} onStart={() => screenRef.current?.offsetHeight ?? 0} onResize={(h) => onVideoH(clamp(h, VIDEO_H_MIN, videoHMax ?? Math.max(VIDEO_H_MIN, window.innerHeight - 160)))} onReset={() => onVideoH(null)} />
      {videoError && <p className="error-text small">视频加载失败，请确认后端已启动且视频文件存在。</p>}
      {ready && trackError && <p className="error-text small">字幕文件加载失败</p>}
    </section>
  )
}
