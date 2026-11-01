import ThemeToggle from '../components/ThemeToggle.tsx'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import {
  errorText,
  formatTime,
  frameUrl,
  getKeyframes,
  imageName,
  slideImageUrl,
  type KeyframesResult,
  type KeyframeSlide,
  type Roi,
} from '../api.ts'
import KeyframeTimeline, { slideColor } from '../components/KeyframeTimeline.tsx'
import RoiEditor from '../components/RoiEditor.tsx'
import { findStage, useVideoProgress } from '../useVideoProgress.ts'

const SPANS = [
  { label: '全部', value: 0 },
  { label: '30 分钟', value: 1800 },
  { label: '10 分钟', value: 600 },
  { label: '2 分钟', value: 120 },
]

export default function KeyframesPageRoute() {
  const { id = '' } = useParams()
  return <KeyframesPage key={id} id={id} />
}

function totalShown(s: KeyframeSlide) {
  return s.appearances.reduce((sum, [a, b]) => sum + (b - a), 0)
}

const pct = (v: number) => `${Math.round(v * 1000) / 10}%`

/** 第 j 次出现的截图名（S128_2）；旧版本抽帧没有 appearance_images 时都是页截图 */
function appearanceImage(s: KeyframeSlide, j: number): string {
  const img = s.appearance_images?.[j]
  return img ? imageName(img) : s.id
}

/** 原始画面上叠加 ROI（虚线）与内容框（实线）；内容框是 ROI 内的相对坐标 */
function FrameOverlay({ roi, box }: { roi: Roi | null; box: Roi | null }) {
  const r: Roi = roi ?? [0, 0, 1, 1]
  const abs: Roi | null = box ? [r[0] + box[0] * r[2], r[1] + box[1] * r[3], box[2] * r[2], box[3] * r[3]] : null
  const style = (b: Roi) => ({
    left: `${b[0] * 100}%`,
    top: `${b[1] * 100}%`,
    width: `${b[2] * 100}%`,
    height: `${b[3] * 100}%`,
  })
  return (
    <>
      {roi && <div className="kf-overlay kf-overlay-roi" style={style(r)} />}
      {abs && <div className="kf-overlay kf-overlay-box" style={style(abs)} />}
    </>
  )
}

function KeyframesPage({ id }: { id: string }) {
  const { video, notFound, live, refresh } = useVideoProgress(id)
  const kfStage = findStage(video, 'keyframes')
  const [data, setData] = useState<KeyframesResult | null>(null)
  const [error, setError] = useState<string | null>(null)
  const videoRef = useRef<HTMLVideoElement>(null)
  const [currentTime, setCurrentTime] = useState(0)
  const [selected, setSelected] = useState<string | null>(null)
  /** 选中幻灯片的第几次出现（决定预览哪张截图） */
  const [selectedApp, setSelectedApp] = useState(0)
  const [hoverT, setHoverT] = useState<number | null>(null)
  const [previewT, setPreviewT] = useState<number | null>(null)
  const [spanSel, setSpanSel] = useState(600)
  const [windowStart, setWindowStart] = useState(0)
  const [roiOpen, setRoiOpen] = useState(false)

  // 抽帧阶段完成（包括重新抽帧后）时加载结果；图片带版本参数防缓存
  const version = kfStage?.status === 'done' ? kfStage.finished_at : null
  useEffect(() => {
    if (!version) return
    let cancelled = false
    getKeyframes(id).then(
      (r) => {
        if (cancelled) return
        setData(r)
        setError(null)
      },
      (e) => !cancelled && setError(errorText(e)),
    )
    return () => {
      cancelled = true
    }
  }, [id, version])

  useEffect(() => {
    document.title = `抽帧展示台 - ${video?.title ?? ''} - LectureTrace`
  }, [video?.title])

  // 悬停曲线时截图：停顿 150ms 再请求，按秒取整便于缓存
  useEffect(() => {
    if (hoverT == null) return
    const timer = setTimeout(() => setPreviewT(Math.round(hoverT)), 150)
    return () => clearTimeout(timer)
  }, [hoverT])

  const slides = data?.slides ?? []
  const debug = data?.debug ?? null
  const duration = debug?.duration_s || video?.duration_s || 0
  const span = spanSel === 0 || spanSel > duration ? duration : spanSel
  const slideIndex = useMemo(() => new Map(slides.map((s, i) => [s.id, i])), [slides])

  const current = useMemo(
    () => slides.find((s) => s.appearances.some(([a, b]) => currentTime >= a && currentTime < b))?.id ?? null,
    [slides, currentTime],
  )

  const seek = useCallback(
    (t: number) => {
      const v = videoRef.current
      if (!v) return
      v.currentTime = Math.max(0, t)
      setCurrentTime(t)
      // 播放位置移出放大窗口时，让窗口跟过去
      if (span > 0 && (t < windowStart || t > windowStart + span)) {
        setWindowStart(Math.min(Math.max(0, t - span / 4), Math.max(0, duration - span)))
      }
    },
    [span, windowStart, duration],
  )

  const stats = useMemo(() => {
    if (!debug) return null
    const count = (k: string) => debug.runs.filter((r) => r.kind === k).length
    const covered = slides.reduce((s, x) => s + totalShown(x), 0)
    return {
      merged: count('merged'),
      revisit: count('revisit'),
      shortCount: debug.short_runs.length,
      shortTotal: debug.short_runs.reduce((s, [a, b]) => s + (b - a), 0),
      blankCount: debug.blank_runs?.length ?? null,
      blankTotal: (debug.blank_runs ?? []).reduce((s, [a, b]) => s + (b - a), 0),
      coverage: duration ? covered / duration : 0,
    }
  }, [debug, slides, duration])

  if (notFound) {
    return (
      <div className="page-center">
        <p>视频不存在或已被删除。</p>
        <Link to="/">返回首页</Link>
      </div>
    )
  }

  // 区域未确认时抽帧不会开始（0001 §2.5），不显示"正在抽帧"
  const roiPending = video?.status === 'awaiting_roi' || video?.roi_confirmed === false
  const kfRunning = kfStage?.status === 'running'
  const running = kfRunning || (kfStage?.status === 'pending' && !roiPending)
  const progress = live.keyframes?.progress
  const selectedSlide = slides.find((s) => s.id === selected) ?? null
  const roi = data?.roi ?? null
  const usedRoi = debug?.roi ?? null
  const fullFrame = (r: typeof roi) => !r || (r[0] === 0 && r[1] === 0 && r[2] === 1 && r[3] === 1)

  return (
    <div className="kf-page">
      <header className="vp-head">
        <Link to={`/videos/${id}`} className="back-link">
          ← 返回视频
        </Link>
        <h1 className="vp-title ellipsis">抽帧展示台 · {video?.title ?? ''}</h1>
        <span className="spacer" />
        <ThemeToggle />
        <button className="btn btn-small" onClick={() => setRoiOpen(true)} disabled={!video || kfRunning}>
          {roiPending ? '框选幻灯片区域' : '修改幻灯片区域'}
        </button>
      </header>

      {kfStage?.status === 'failed' && <div className="alert alert-error">抽帧失败：{kfStage.error}</div>}
      {roiPending && kfStage?.status === 'pending' && (
        <div className="alert alert-warn">
          等待确认幻灯片区域：点右上角"框选幻灯片区域"确认后开始抽帧。
        </div>
      )}
      {running && (
        <div className="alert alert-info">
          正在抽帧{progress != null ? `（${Math.round(progress * 100)}%）` : ''}，完成后自动刷新。
          {progress != null && (
            <div className="progress progress-thin">
              <div className="progress-bar" style={{ width: `${progress * 100}%` }} />
            </div>
          )}
        </div>
      )}
      {error && <p className="error-text">{error}</p>}

      {data && (
        <section className="kf-stats">
          <div>
            <b>{slides.length}</b> 张幻灯片
          </div>
          {stats && (
            <>
              <div title="要点逐条出现等情况，被合并到上一张">
                <b>{stats.merged}</b> 次合并
              </div>
              <div title="老师翻回之前的页">
                <b>{stats.revisit}</b> 次翻回
              </div>
              <div title="停留不足稳定时长、被丢弃的画面；时间轴上红色标出">
                <b className="error-text">{stats.shortCount}</b> 段短暂画面（共 {formatTime(stats.shortTotal)}）
              </div>
              {stats.blankCount != null && (
                <div
                  title={
                    '内容太少（纯黑、空白页等）的稳定画面，不算幻灯片，按无幻灯片时段处理；时间轴上灰色标出' +
                    (debug?.blank_runs?.length
                      ? '\n' +
                        debug.blank_runs
                          .slice(0, 20)
                          .map(([a, b, ink]) => `${formatTime(a)}–${formatTime(b)}（内容 ${ink}%）`)
                          .join('\n')
                      : '')
                  }
                >
                  <b>{stats.blankCount}</b> 段空白已丢弃（共 {formatTime(stats.blankTotal)}）
                </div>
              )}
              <div>
                覆盖 <b>{Math.round(stats.coverage * 100)}%</b> 时长
              </div>
            </>
          )}
          <div className="muted small">
            幻灯片区域：{fullFrame(usedRoi) ? '整个画面' : `已框选（${usedRoi!.map((v) => Math.round(v * 100)).join(', ')}%）`}
            {JSON.stringify(usedRoi) !== JSON.stringify(roi) && !running && (
              <span className="warn-text">；设置已变更，尚未按新区域重新抽帧</span>
            )}
            {debug?.content_box && (
              <span title="在幻灯片区域内自动去掉黑边后的范围；比对和保存的截图都只用这部分。悬停曲线时在画面上以实线框标出">
                ；内容框：
                {debug.content_box_fallback
                  ? '未发现明显黑边，使用整个区域'
                  : `x ${pct(debug.content_box[0])}，y ${pct(debug.content_box[1])}，宽 ${pct(debug.content_box[2])}，高 ${pct(debug.content_box[3])}（相对区域）`}
              </span>
            )}
          </div>
        </section>
      )}

      <section className="kf-top">
        <div className="kf-player">
          <video
            ref={videoRef}
            src={`/api/videos/${id}/file`}
            controls
            preload="metadata"
            onTimeUpdate={(e) => {
              const t = e.currentTarget.currentTime
              setCurrentTime((prev) => (Math.abs(prev - t) >= 0.5 ? t : prev))
            }}
          />
        </div>
        <div className="kf-preview">
          {hoverT != null && previewT != null ? (
            <>
              <div className="kf-frame">
                <img src={frameUrl(id, previewT, 640)} alt="悬停位置的画面" />
                {debug && (
                  <FrameOverlay
                    roi={fullFrame(usedRoi) ? null : usedRoi}
                    box={debug.content_box && !debug.content_box_fallback ? debug.content_box : null}
                  />
                )}
              </div>
              <p className="small mono">
                悬停位置 {formatTime(previewT)}（原始画面
                {debug?.content_box ? '；虚线为幻灯片区域，实线为内容框' : ''}）
              </p>
            </>
          ) : selectedSlide ? (
            <>
              <img
                src={slideImageUrl(id, appearanceImage(selectedSlide, selectedApp), version)}
                alt={selectedSlide.id}
              />
              <p className="small">
                <b>{selectedSlide.id}</b>
                {selectedSlide.appearances.length > 1 ? `.${selectedApp + 1}` : ''}（保存的关键帧
                {selectedSlide.appearances.length > 1 ? `，第 ${selectedApp + 1} 次出现` : ''}）
              </p>
            </>
          ) : (
            <p className="muted small">悬停下方曲线可查看任意时刻的画面；点击幻灯片卡片可查看保存的关键帧。</p>
          )}
        </div>
      </section>

      {debug && duration > 0 && (
        <section className="panel kf-chart">
          <div className="panel-head">
            <h2>画面变化曲线</h2>
            <div className="segmented">
              {SPANS.map((s) => (
                <button
                  key={s.label}
                  className={spanSel === s.value ? 'is-on' : ''}
                  onClick={() => {
                    setSpanSel(s.value)
                    setWindowStart((w) => (s.value === 0 ? 0 : Math.min(w, Math.max(0, duration - s.value))))
                  }}
                >
                  {s.label}
                </button>
              ))}
            </div>
          </div>
          <div className="panel-body">
            <KeyframeTimeline
              debug={debug}
              slideIndex={slideIndex}
              duration={duration}
              windowStart={span >= duration ? 0 : windowStart}
              span={span}
              currentTime={currentTime}
              onWindowStart={setWindowStart}
              onHover={setHoverT}
              onSeek={seek}
            />
            <p className="muted small kf-legend">
              上：全片总览，色块为各幻灯片的出现时段，底部红色为被丢弃的短暂画面；点击或拖动选择放大区间。
              下：每秒画面变化（变化像素占比，平方根刻度）；红色虚线为换页阈值，浅红底为短暂画面，竖线为切分点，色带中
              ↺ 表示翻回旧页、浅色表示合并段。悬停查看画面，点击跳转播放，滚轮平移。
            </p>
          </div>
        </section>
      )}
      {data && !debug && (
        <div className="alert alert-warn">这次抽帧没有记录画面变化曲线（旧版本结果），重新框选区域或重跑抽帧后可查看。</div>
      )}

      <section className="kf-grid">
        {slides.map((s, i) => (
          <article
            key={s.id}
            className={`kf-card${s.id === current ? ' kf-card-current' : ''}${s.id === selected ? ' kf-card-selected' : ''}`}
            style={{ borderTopColor: slideColor(i) }}
            onClick={() => {
              setSelected(s.id)
              setSelectedApp(0)
            }}
          >
            <img src={slideImageUrl(id, s.id, version)} alt={s.id} loading="lazy" />
            <div className="kf-card-body">
              <div className="kf-card-head">
                <b className="mono">{s.id}</b>
                <span className="muted small">共 {formatTime(totalShown(s))}</span>
              </div>
              <div className="kf-apps">
                {s.appearances.map(([a, b], j) => (
                  <button
                    key={j}
                    className={`ts-badge${s.id === selected && j === selectedApp && j > 0 ? ' is-on' : ''}`}
                    onClick={(e) => {
                      e.stopPropagation()
                      setSelected(s.id)
                      setSelectedApp(j)
                      seek(a)
                    }}
                    title={
                      j > 0
                        ? `翻回此页（${s.id}.${j + 1}）${s.appearance_images?.[j] ? '，预览区显示这次的截图' : ''}`
                        : undefined
                    }
                  >
                    {j > 0 && '↺ '}
                    {formatTime(a)}–{formatTime(b)}
                  </button>
                ))}
              </div>
            </div>
          </article>
        ))}
      </section>

      {roiOpen && (
        <RoiEditor
          videoId={id}
          open
          time={currentTime > 0 ? currentTime : Math.min(60, (video?.duration_s ?? 120) / 2)}
          duration={video?.duration_s}
          initial={data ? (fullFrame(roi) ? null : roi) : fullFrame(video?.roi_suggestion ?? null) ? null : (video?.roi_suggestion ?? null)}
          confirmLabel={kfStage?.status === 'done' || kfStage?.status === 'failed' ? '保存并重新抽帧' : '确认并开始抽帧'}
          onClose={() => setRoiOpen(false)}
          onSaved={() => {
            setRoiOpen(false)
            setData(null)
            setSelected(null)
            void refresh()
          }}
        />
      )}
    </div>
  )
}
