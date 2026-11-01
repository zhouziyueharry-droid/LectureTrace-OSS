import { useCallback, useEffect, useState } from 'react'
import {
  ApiError,
  errorText,
  getVideo,
  subscribeEvents,
  type ProgressEvent,
  type StageInfo,
  type StageName,
  type VideoDetail,
} from './api.ts'

export interface StageLive {
  progress?: number
  message?: string
}

export type ConnState = 'connecting' | 'open' | 'reconnecting'

/** 视频详情 + SSE 实时进度 */
export function useVideoProgress(id: string) {
  const [video, setVideo] = useState<VideoDetail | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [notFound, setNotFound] = useState(false)
  const [live, setLive] = useState<Partial<Record<StageName, StageLive>>>({})
  const [conn, setConn] = useState<ConnState>('connecting')

  const refresh = useCallback(async () => {
    try {
      const v = await getVideo(id)
      setVideo(v)
      setLoadError(null)
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) setNotFound(true)
      else setLoadError(errorText(e))
    }
  }, [id])

  useEffect(() => {
    setVideo(null)
    setNotFound(false)
    setLoadError(null)
    setLive({})
    setConn('connecting')
    void refresh()

    const apply = (ev: ProgressEvent) => {
      if (ev.type === 'snapshot') {
        setVideo(ev.video)
        setLoadError(null)
        setLive((prev) => {
          const next: Partial<Record<StageName, StageLive>> = {}
          for (const s of ev.video.stages ?? []) if (s.status === 'running' && prev[s.name]) next[s.name] = prev[s.name]
          return next
        })
      } else if (ev.type === 'stage') {
        const now = new Date().toISOString()
        setVideo((v) => {
          if (!v) return v
          let found = false
          const stages = v.stages.map((s): StageInfo => {
            if (s.name !== ev.stage) return s
            found = true
            const changed = s.status !== ev.status
            return {
              ...s,
              status: ev.status,
              error: ev.status === 'failed' ? (ev.error ?? s.error) : (ev.error ?? null),
              started_at: ev.status === 'running' && changed ? now : s.started_at,
              finished_at:
                (ev.status === 'done' || ev.status === 'failed' || ev.status === 'skipped') && changed
                  ? now
                  : ev.status === 'running'
                    ? null
                    : s.finished_at,
              attempt: ev.status === 'running' && changed ? s.attempt + 1 : s.attempt,
            }
          })
          if (!found) {
            stages.push({
              name: ev.stage,
              status: ev.status,
              error: ev.error ?? null,
              started_at: ev.status === 'running' ? now : null,
              finished_at: ev.status === 'done' ? now : null,
              attempt: 1,
            })
          }
          // 阶段重新跑完后，不再算作"参数已变化"
          const stale = ev.status === 'done' ? v.stale_stages.filter((n) => n !== ev.stage) : v.stale_stages
          return { ...v, stages, stale_stages: stale }
        })
        setLive((prev) => ({
          ...prev,
          [ev.stage]: ev.status === 'running' ? { progress: ev.progress, message: ev.message } : undefined,
        }))
      } else if (ev.type === 'video') {
        setVideo((v) => (v ? { ...v, status: ev.status } : v))
      }
    }

    const close = subscribeEvents(id, apply, (state) => setConn(state))
    return close
  }, [id, refresh])

  return { video, loadError, notFound, live, conn, refresh, setVideo }
}

export function findStage(video: VideoDetail | null, name: StageName): StageInfo | undefined {
  return video?.stages?.find((s) => s.name === name)
}
