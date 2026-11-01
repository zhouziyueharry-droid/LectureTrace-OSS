import { useState } from 'react'
import {
  STAGE_NAMES,
  STAGE_STATUS_LABELS,
  stageLabel,
  videoStatusLabel,
  type Roi,
  type StageInfo,
  type StageName,
  type VideoDetail,
} from '../api.ts'
import type { ConnState, StageLive } from '../useVideoProgress.ts'

interface Props {
  video: VideoDetail
  live: Partial<Record<StageName, StageLive>>
  conn: ConnState
  retrying: boolean
  retryError: string | null
  onRetry: (fromStage?: StageName) => void
  onOpenPlan: () => void
  onRunOutline: () => void
  /** 打开"框选幻灯片区域"（修改区域） */
  onEditRoi: () => void
  /** 从顶栏「处理详情」打开时直接展开各阶段 */
  startOpen?: boolean
}

/** 需要调用 GPT 的三个阶段；未开启时被跳过，可在这里单独运行（与后端 runner.OUTLINE_STAGES 一致，不含 segments） */
const OUTLINE_STAGES: StageName[] = ['read_slides', 'evidence', 'outline']
const DISABLED_REASONS = ['未开启大纲生成', '暂未启用']
/** 重跑某阶段时会连带重跑的下游（与后端 runner.DEPENDS 一致） */
const DOWNSTREAM: Partial<Record<StageName, StageName[]>> = {
  extract_audio: ['asr', 'cues', 'segments', 'translate', 'read_slides', 'evidence', 'outline'],
  asr: ['cues', 'segments', 'translate', 'read_slides', 'evidence', 'outline'],
  cues: ['segments', 'translate', 'read_slides', 'evidence', 'outline'],
  keyframes: ['segments', 'read_slides', 'evidence', 'outline'],
  segments: ['read_slides', 'evidence', 'outline'],
  translate: [],
  read_slides: ['evidence', 'outline'],
  evidence: ['outline'],
  outline: [],
}

const STATUS_ICON: Record<string, string> = {
  pending: '○',
  running: '◐',
  done: '✓',
  failed: '✕',
  skipped: '–',
}

const ROI_WAIT_MESSAGE = '等待确认幻灯片区域'

function roiText(roi: Roi | null): string {
  if (!roi || (roi[0] === 0 && roi[1] === 0 && roi[2] === 1 && roi[3] === 1)) return '整个画面'
  return `已框选（x ${Math.round(roi[0] * 100)}%，y ${Math.round(roi[1] * 100)}%，宽 ${Math.round(roi[2] * 100)}%，高 ${Math.round(roi[3] * 100)}%）`
}

/** 全部阶段完成且没有失败、没有过期结果：处理栏可以收起，只在顶栏留一个入口 */
export function stageSettled(video: VideoDetail): boolean {
  const stages = video.stages ?? []
  return video.status === 'done' && stages.length > 0 && stages.every((s) => s.status === 'done' || s.status === 'skipped')
    && !(video.stale_stages ?? []).length
}

export default function StagePanel({
  video,
  live,
  conn,
  retrying,
  retryError,
  onRetry,
  onOpenPlan,
  onRunOutline,
  onEditRoi,
  startOpen,
}: Props) {
  const [expanded, setExpanded] = useState<boolean | null>(startOpen ? true : null)

  const byName = new Map<string, StageInfo>((video.stages ?? []).map((s) => [s.name, s]))
  const ordered: StageInfo[] = [
    ...STAGE_NAMES.map(
      (n) =>
        byName.get(n) ?? { name: n, status: 'pending' as const, started_at: null, finished_at: null, error: null, attempt: 0 },
    ),
    // 后端若多出未知阶段，也显示出来
    ...(video.stages ?? []).filter((s) => !(STAGE_NAMES as readonly string[]).includes(s.name)),
  ]

  const failed = ordered.filter((s) => s.status === 'failed')
  const running = ordered.find((s) => s.status === 'running')
  const doneCount = ordered.filter((s) => s.status === 'done' || s.status === 'skipped').length
  const allDone = doneCount === ordered.length
  const canRetry = video.status === 'failed' || failed.length > 0
  const stale = video.stale_stages ?? []
  const firstStale = STAGE_NAMES.find((n) => stale.includes(n)) ?? stale[0]
  const isOpen = expanded ?? !allDone
  const idle = video.status === 'done' || video.status === 'failed'
  const outlineOff = OUTLINE_STAGES.some((n) => {
    const st = byName.get(n)
    return st?.status === 'skipped' && DISABLED_REASONS.includes(st.error ?? '')
  })

  const rerun = (name: StageName) => {
    const after = (DOWNSTREAM[name] ?? []).filter((n) => {
      const st = byName.get(n)
      return st && !(st.status === 'skipped' && DISABLED_REASONS.includes(st.error ?? ''))
    })
    const tail = after.length ? `，并连带重跑：${after.map(stageLabel).join('、')}` : ''
    const paid = [name, ...after].some((n) => OUTLINE_STAGES.includes(n) || n === 'translate')
    const note = paid ? '（涉及外部服务，开始前会先显示发送清单与费用预估）' : ''
    if (window.confirm(`重跑「${stageLabel(name)}」${tail}？${note}`)) onRetry(name)
  }

  // 旧后端没有 roi_confirmed 字段时按已确认处理
  const roiPending = video.status === 'awaiting_roi' || video.roi_confirmed === false
  const keyframesRunning = byName.get('keyframes')?.status === 'running'

  let summary: string
  if (failed.length) summary = `${stageLabel(failed[0].name)}失败`
  else if (video.status === 'awaiting_roi') summary = '字幕已完成，等待你框选幻灯片区域后开始抽帧'
  else if (video.status === 'awaiting_confirm') summary = '本机处理完成，等待你确认发送清单'
  else if (running) {
    const p = live[running.name]?.progress
    summary = `正在${stageLabel(running.name)}${p != null ? ` ${Math.round(p * 100)}%` : '…'}`
  } else if (allDone) summary = '全部处理完成'
  else summary = `已完成 ${doneCount}/${ordered.length} 个阶段`

  return (
    <section className={`stage-panel${isOpen ? ' is-open' : ''}`} aria-label="处理状态">
      <div className="stage-summary">
        <span className={`status-pill st-${video.status}`}>{videoStatusLabel(video.status)}</span>
        <span className="stage-summary-text">{summary}</span>
        <div className="overall-progress" aria-hidden>
          <div className="progress-bar" style={{ width: `${(doneCount / ordered.length) * 100}%` }} />
        </div>
        {conn === 'reconnecting' && <span className="conn-warn small">与后端的连接中断，正在重连…</span>}
        <div className="stage-summary-actions">
          {video.status === 'awaiting_confirm' && (
            <button className="btn btn-small btn-primary" onClick={onOpenPlan}>
              查看发送清单
            </button>
          )}
          {outlineOff && idle && (
            <button className="btn btn-small btn-primary" onClick={onRunOutline} disabled={retrying}
              title="读取幻灯片 → 整理证据 → 生成大纲；确认发送前会显示费用预估">
              运行大纲生成（7–9）
            </button>
          )}
          {canRetry && (
            <button className="btn btn-small btn-primary" onClick={() => onRetry()} disabled={retrying}>
              {retrying ? '重试中…' : '重试'}
            </button>
          )}
          <button className="btn btn-small" onClick={() => setExpanded(!isOpen)} aria-expanded={isOpen}>
            {isOpen ? '收起详情' : '展开详情'}
          </button>
        </div>
      </div>

      {retryError && <p className="error-text small">重试失败：{retryError}</p>}

      {failed.map((s) => (
        <div key={s.name} className="alert alert-error small">
          <strong>{stageLabel(s.name)}失败：</strong>
          {s.error || '未知原因'}
          {s.attempt > 1 && <span className="muted">（已尝试 {s.attempt} 次）</span>}
        </div>
      ))}

      {stale.length > 0 && (
        <div className="alert alert-info small">
          参数已变化，部分结果可重新处理：{stale.map(stageLabel).join('、')}
          <button className="btn btn-small" onClick={() => onRetry(firstStale)} disabled={retrying} style={{ marginLeft: 8 }}>
            重新处理
          </button>
        </div>
      )}

      {isOpen && video.roi_confirmed && (
        <div className="roi-line small">
          <span className="muted">幻灯片区域：{roiText(video.roi)}</span>
          <button
            className="btn-text small stage-rerun"
            onClick={onEditRoi}
            disabled={keyframesRunning}
            title={keyframesRunning ? '正在抽帧，结束后才能修改' : '修改后只重跑抽帧及其下游，语音识别和字幕不重跑'}
          >
            修改区域
          </button>
        </div>
      )}

      {isOpen && (
        <ol className="stage-list">
          {ordered.map((s, i) => {
            const lv = live[s.name]
            const pct = lv?.progress != null ? Math.round(lv.progress * 100) : null
            // await_confirm 是暂停点，没有进度可言
            const isWaiting = s.name === 'await_confirm' && s.status === 'running'
            const statusText = isWaiting
              ? '待确认'
              : s.status === 'running' && pct != null
                ? `${pct}%`
                : (STAGE_STATUS_LABELS[s.status] ?? s.status)
            return (
              <li
                key={s.name}
                className={`stage-item stage-${s.status}${stale.includes(s.name) ? ' is-stale' : ''}`}
                title={`${stageLabel(s.name)}：${statusText}`}
              >
                <div className="stage-line">
                  <span className={`stage-icon${isWaiting ? ' is-waiting' : ''}`} aria-hidden>
                    {isWaiting ? '!' : (STATUS_ICON[s.status] ?? '○')}
                  </span>
                  <span className="stage-name">
                    {i + 1}. {stageLabel(s.name)}
                  </span>
                  {s.status !== 'done' && s.status !== 'pending' && <span className="stage-status small">{statusText}</span>}
                </div>
                {s.status === 'running' && !isWaiting && (
                  <div className={`progress progress-thin${pct == null ? ' indeterminate' : ''}`}>
                    <div className="progress-bar" style={{ width: pct == null ? '40%' : `${pct}%` }} />
                  </div>
                )}
                {s.status === 'running' && lv?.message && <div className="stage-msg small muted">{lv.message}</div>}
                {s.status === 'skipped' && s.error && <div className="stage-msg small muted">{s.error}</div>}
                {s.name === 'keyframes' && s.status === 'pending' && roiPending && (
                  <div className="stage-msg small muted">{ROI_WAIT_MESSAGE}</div>
                )}
                {idle && s.name !== 'await_confirm' && (s.status === 'done' || s.status === 'failed') && (
                  <button className="stage-rerun btn-text small" onClick={() => rerun(s.name)} disabled={retrying}>
                    重跑
                  </button>
                )}
              </li>
            )
          })}
        </ol>
      )}
    </section>
  )
}
