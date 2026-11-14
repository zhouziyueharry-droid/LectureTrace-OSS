import { tr, systemText } from './i18n.ts'
/**
 * 后端接口与数据类型。类型严格对应 docs/CONTRACTS.md 与 ARCHITECTURE.md §6。
 * 所有请求失败时抛 ApiError，message 为后端返回的 detail（中文原因）。
 */

// ---------- 基础类型 ----------

export const STAGE_NAMES = [
  'extract_audio',
  'keyframes',
  'asr',
  'cues',
  'segments',
  'await_confirm',
  'translate',
  'read_slides',
  'evidence',
  'outline',
] as const

export type StageName = (typeof STAGE_NAMES)[number]

export const STAGE_LABELS: Record<StageName, string> = {
  get extract_audio() { return tr("抽取音频") },
  get asr() { return tr("语音识别") },
  get cues() { return tr("切分字幕") },
  get keyframes() { return tr("提取幻灯片") },
  get segments() { return tr("切分讲解片段") },
  get await_confirm() { return tr("等待确认") },
  get translate() { return tr("翻译字幕") },
  get read_slides() { return tr("读取幻灯片") },
  get evidence() { return tr("整理证据") },
  get outline() { return tr("生成大纲") },
}

export function stageLabel(name: string): string {
  return (STAGE_LABELS as Record<string, string>)[name] ?? name
}

export type StageStatus = 'pending' | 'running' | 'done' | 'failed' | 'skipped'

export const STAGE_STATUS_LABELS: Record<StageStatus, string> = {
  get pending() { return tr("等待中") },
  get running() { return tr("进行中") },
  get done() { return tr("已完成") },
  get failed() { return tr("失败") },
  get skipped() { return tr("已跳过") },
}

/** awaiting_roi：语音一侧已处理完，等待用户确认幻灯片区域后才抽帧（提案 0001 §2.5） */
export type VideoStatus = 'processing' | 'awaiting_roi' | 'awaiting_confirm' | 'done' | 'failed'

export const VIDEO_STATUS_LABELS: Record<VideoStatus, string> = {
  get processing() { return tr("处理中") },
  get awaiting_roi() { return tr("等待确认区域") },
  get awaiting_confirm() { return tr("等待确认") },
  get done() { return tr("已完成") },
  get failed() { return tr("失败") },
}

export function videoStatusLabel(s: string): string {
  return (VIDEO_STATUS_LABELS as Record<string, string>)[s] ?? s
}

/** GET /api/videos 的列表项 */
export interface Video {
  id: string
  title: string
  filename: string
  duration_s: number | null
  created_at: string
  status: VideoStatus
  course_id: string | null
}

export interface Course {
  id: string
  title: string
  created_at: string
}

export interface StageInfo {
  name: StageName
  status: StageStatus
  started_at: string | null
  finished_at: string | null
  error: string | null
  attempt: number
}

/** GET /api/videos/{id} */
export interface VideoDetail extends Video {
  stages: StageInfo[]
  stale_stages: StageName[]
  /** 幻灯片区域是否已确认；未确认时不抽帧（旧视频已有抽帧结果时视为已确认） */
  roi_confirmed: boolean
  /** 当前使用的区域；null 表示整个画面或尚未确认 */
  roi: Roi | null
  /** 框选时的预填建议：该视频上次保存的框，其次全局默认；null 表示整个画面 */
  roi_suggestion: Roi | null
}

/** 发送清单中的一项 */
export interface SendPlanItem {
  local?: boolean
  stage: string
  label?: string
  provider?: string
  desc: string
  /** null：无法估算（如未填写 Key）或暂未启用 */
  estimated_cost_usd: number | null
  skipped?: boolean
  chars?: number
  /** GPT 阶段：模型、输入 / 输出 token 区间（输出含推理）、图片 token、费用区间（美元） */
  model?: string
  effort?: string | null
  input_tokens_range?: [number, number]
  output_tokens_range?: [number, number]
  image_tokens?: number
  cost_usd_range?: [number, number] | null
  /** 实际计价档位（接口不是 OpenAI 官方时 flex 不生效，为 standard） */
  service_tier?: ServiceTier
  /** flex 档：资源不足、全部改用标准档时的费用上限（美元） */
  fallback_cost_usd_max?: number
}

/** GET /api/videos/{id}/send-plan */
export interface SendPlan {
  cost_complete?: boolean
  items: SendPlanItem[]
  estimated_cost_usd: number
  text_chars: number
  slide_count: number
  /** 提醒，不阻止发送 */
  warnings: string[]
  /** 非空时不能确认发送（如未填写 Key、免费额度不足） */
  blocking: string[]
  deepl_usage: { used: number; limit: number } | null
  cost_usd_range: [number, number]
  cost_sgd_range: [number, number]
  /** 最坏情况：flex 阶段全部改用标准档（按标准价、取输出上限），其余阶段取区间上限 */
  fallback_cost_usd_max?: number
  fallback_cost_sgd_max?: number
  usd_to_sgd: number
  prices_as_of: string
  flex_prices_as_of?: string
  provider: string
  translator: string
}

/** outline.json 的 data（交接 0001 §7.2） */
export interface OutlineNode {
  node_id?: string
  title: string
  summary: string
  /** 证据 ID：S012、S012.2、N001、T0031 */
  evidence: string[]
  /** 所引证据中最早的跳转时间（秒） */
  start: number
  children?: OutlineNode[]
}

export interface OutlineAdmin {
  title: string
  items: OutlineNode[]
}

/** T 的类别（无页讲解片段由大纲给出分类） */
export type SpeechCategory = 'lecture' | 'admin' | 'interaction' | 'media' | 'offtopic' | 'mixed' | 'uncertain'

export interface Outline {
  revision?: string
  chapters: OutlineNode[]
  /** 课程事务：作业截止、考试安排等；旧版大纲没有，为 null */
  admin: OutlineAdmin | null
  /** 无页 T 的分类，字典 {"T0040": "media"} */
  speech_categories: Record<string, SpeechCategory | string>
  warnings: string[]
}

/** slides.json 的 data（可选接口） */
export interface Slide {
  id: string
  title: string
  bullets: string[]
  formulas: string[]
  figure: string
  emphasis: string
  appearances: [number, number][]
}

export interface Citation {
  id: string
  start?: number
  pdf_id?: string
  revision?: string
  page?: number
  title?: string
  url?: string
}

export interface ChatMessage {
  id: number | string
  role: 'user' | 'assistant'
  content: string
  citations: Citation[]
  created_at: string
  /** failed：引用校验失败等，未作为正常回答保存（交接 0001 §10.2）；旧后端没有此字段，按 ok 处理 */
  status?: 'ok' | 'failed'
  /** 证据已重跑，这条回答的引用可能指向新内容：citations 已置空，停用时间戳跳转 */
  stale?: boolean
  /** 失败原因（后端若提供） */
  error?: string | null
}

/** 定位后实际展开的内容：t 段讲解原文、images 张截图 */
export interface ChatExpanded {
  t: number
  images: number
}

/** POST /api/videos/{id}/chat 的 SSE 事件 */
export interface ContextSnapshot {
  thread_id: string
  turn_id: string
  model: string
  context_window: number | null
  last: Record<'inputTokens' | 'cachedInputTokens' | 'outputTokens' | 'reasoningOutputTokens' | 'totalTokens', number | null>
  usage: Record<string, unknown>
  updated_at: string
}

export type ChatEvent =
  | { type: 'interaction'; interaction: AgentInteraction | null }
  | { type: 'context'; context: ContextSnapshot | null; previous?: boolean }
  | { type: 'compaction'; active: boolean }
  | { type: 'activity'; text: string }
  | { type: 'progress'; text: string }
  | { type: 'commentary'; text: string; append: boolean }
  | { type: 'note_changed' }
  | { type: 'usage'; usage: Record<string, unknown> }
  | { type: 'outline_preview'; preview: OutlinePreview }
  | { type: 'phase'; phase: 'locate' }
  | { type: 'phase'; phase: 'answer'; expanded?: ChatExpanded }
  | { type: 'delta'; text: string }
  /** 引用自动修正后的完整回答，替换正在显示的内容 */
  | { type: 'replace'; text: string }
  | { type: 'done'; message: ChatMessage }
  | { type: 'error'; error: string }

export interface ChatRun {
  id: string
  conversation_id: string
  question: string
  options: { node_id?: string; allow_outline_edit?: boolean; note_target?: NoteTarget; pdf_target?: PdfTarget }
  status: 'running' | 'done' | 'error'
  created_at: string
  pending: { phase: 'start' | 'locate' | 'answer'; expanded: ChatExpanded | null; text: string; corrected: boolean; activity: string[]; usage?: Record<string, unknown>; progress?: string; commentary?: string; note_updates?: number; interaction?: AgentInteraction | null }
  context: ContextSnapshot | null
  context_current: boolean
  compacting: boolean
  preview: OutlinePreview | null
  message: ChatMessage | null
  error: string | null
}

/** GET /api/videos/{id}/events 的 SSE 事件 */
export type ProgressEvent =
  | { type: 'snapshot'; video: VideoDetail }
  | {
      type: 'stage'
      stage: StageName
      status: StageStatus
      progress?: number
      message?: string
      error?: string | null
    }
  | { type: 'video'; status: VideoStatus }

// ---------- 请求工具 ----------

export class ApiError extends Error {
  status: number
  constructor(message: string, status: number) {
    super(message)
    this.status = status
  }
}

/** 把 FastAPI 的 detail（字符串或校验错误数组）转成可读文本 */
function detailToText(detail: unknown): string | null {
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    const parts = detail
      .map((d) => (d && typeof d === 'object' && 'msg' in d ? String((d as { msg: unknown }).msg) : null))
      .filter(Boolean)
    if (parts.length) return parts.join(tr("；"))
  }
  return null
}

function parseErrorBody(text: string, status: number, statusText: string): ApiError {
  let msg: string | null = null
  try {
    msg = detailToText(JSON.parse(text)?.detail)
  } catch {
    /* 非 JSON */
  }
  if (!msg) {
    if (status === 0) msg = tr("无法连接后端服务，请确认后端已启动")
    else if (status === 502 || status === 503 || status === 504) msg = tr("后端服务不可用（{0}），请确认后端已启动", status)
    else msg = tr("请求失败（{0}{1}）", status, statusText ? ' ' + statusText : '')
  }
  return new ApiError(msg, status)
}

async function toApiError(res: Response): Promise<ApiError> {
  const text = await res.text().catch(() => '')
  return parseErrorBody(text, res.status, res.statusText)
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  let res: Response
  try {
    res = await fetch(url, init)
  } catch {
    throw new ApiError(tr("无法连接后端服务，请确认后端已启动"), 0)
  }
  if (!res.ok) throw await toApiError(res)
  if (res.status === 204) return undefined as T
  const text = await res.text()
  return (text ? JSON.parse(text) : undefined) as T
}

function jsonBody(body: unknown): RequestInit {
  return {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }
}

const base = (id: string) => `/api/videos/${encodeURIComponent(id)}`

// ---------- 视频 ----------

export const listCourses = () => request<Course[]>('/api/courses')
export const createCourse = (title: string) => request<Course>('/api/courses', jsonBody({ title }))
export const renameCourse = (id: string, title: string) =>
  request<Course>(`/api/courses/${encodeURIComponent(id)}`, { ...jsonBody({ title }), method: 'PATCH' })
export const deleteCourse = (id: string) =>
  request<{ ok: boolean }>(`/api/courses/${encodeURIComponent(id)}`, { method: 'DELETE' })

export const listVideos = () => request<Video[]>('/api/videos')

export const getVideo = (id: string) => request<VideoDetail>(base(id))
export const setVideoCourse = (videoId: string, courseId: string | null) =>
  request<VideoDetail>(`${base(videoId)}/course`, { ...jsonBody({ course_id: courseId }), method: 'PUT' })

export const deleteVideo = (id: string) => request<void>(base(id), { method: 'DELETE' })

export const videoFileUrl = (id: string) => `${base(id)}/file`

export const subtitleUrl = (id: string, kind: 'source' | 'bilingual', version: string | number) =>
  `${base(id)}/subtitles/${kind}.vtt?v=${encodeURIComponent(String(version))}`

/** version：抽帧完成时间，重新抽帧后图片同名，需带版本参数防止浏览器缓存。
 * slideId 也可以是某次出现的截图名，如 S128_2（对应出现 S128.2） */
export const slideImageUrl = (id: string, slideId: string, version?: string | null) =>
  `${base(id)}/slides/${encodeURIComponent(slideId)}.jpg${version ? `?v=${encodeURIComponent(version)}` : ''}`

/** "keyframes/S128_2.jpg" → "S128_2" */
export const imageName = (path: string) => path.replace(/^.*\//, '').replace(/\.jpg$/i, '')

export interface UploadHandle {
  promise: Promise<string>
  abort: () => void
}

export interface PackagePreview {
  token: string
  video_id?: string
  course_id?: string
  lectures?: { video_id: string; title: string; duplicate: boolean }[]
  title: string
  course_title: string | null
  has_video: true
  size_bytes: number
  contents: string[]
  note_count: number
  duplicate: boolean
}

export const importPackage = (token: string) =>
  request<{ video_id: string } | { course_id: string; video_ids: string[] }>(`/api/packages/${encodeURIComponent(token)}/import`, jsonBody({}))
export const deletePackage = (token: string) =>
  request<void>(`/api/packages/${encodeURIComponent(token)}`, { method: 'DELETE' })
export interface PackageProgress {
  phase: 'waiting' | 'scanning' | 'writing' | 'verifying'
  written_bytes: number
  total_bytes: number
  lecture_index: number
  lecture_count: number
  lecture_title: string
}
interface PreparedPackage { token: string; download_url: string; filename: string }
type PackageEvent = ({ type: 'progress' } & PackageProgress)
  | { type: 'done'; result: PreparedPackage }
  | { type: 'error'; detail: string; status: number }

async function preparePackageStream(url: string, onProgress: (progress: PackageProgress) => void): Promise<PreparedPackage> {
  let response: Response
  try {
    response = await fetch(url, { ...jsonBody({}), headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' } })
  } catch {
    throw new ApiError(tr("无法连接后端服务，请确认后端已启动"), 0)
  }
  if (!response.ok) throw await toApiError(response)
  let result: PreparedPackage | undefined
  await readEventStream<PackageEvent>(response, (event) => {
    if (event.type === 'progress') onProgress(event)
    if (event.type === 'done') result = event.result
    if (event.type === 'error') throw new ApiError(event.detail, event.status)
  })
  if (!result) throw new ApiError(tr("打包进度连接已中断，未确认打包完成。请重试。"), 0)
  return result
}

export const preparePackage = (id: string, onProgress: (progress: PackageProgress) => void) =>
  preparePackageStream(`${base(id)}/package`, onProgress)
export const prepareCoursePackage = (id: string, onProgress: (progress: PackageProgress) => void) =>
  preparePackageStream(`/api/courses/${encodeURIComponent(id)}/package`, onProgress)

/** Send the File directly: do not buffer a multi-GB archive in JavaScript memory. */
export function previewPackage(file: File, onProgress: (fraction: number) => void): { promise: Promise<PackagePreview>; abort: () => void } {
  const xhr = new XMLHttpRequest()
  const promise = new Promise<PackagePreview>((resolve, reject) => {
    xhr.open('POST', '/api/packages/preview')
    xhr.setRequestHeader('Content-Type', 'application/octet-stream')
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) onProgress(event.loaded / event.total)
    }
    xhr.upload.onload = () => onProgress(1)
    xhr.onload = () => {
      if (xhr.status < 200 || xhr.status >= 300) {
        reject(parseErrorBody(xhr.responseText, xhr.status, xhr.statusText))
        return
      }
      try {
        const data = JSON.parse(xhr.responseText) as PackagePreview
        if (!data.token || !(data.video_id || (data.course_id && data.lectures?.length))) throw new Error()
        resolve(data)
      } catch {
        reject(new ApiError(tr("课程包预览返回格式无法识别，请重试"), xhr.status))
      }
    }
    xhr.onerror = () => reject(new ApiError(tr("上传失败：无法连接后端服务"), 0))
    xhr.onabort = () => reject(new ApiError(tr("上传已取消"), 0))
    xhr.send(file)
  })
  return { promise, abort: () => xhr.abort() }
}

/**
 * 上传 MP4（multipart 字段名 file）。用 XHR 以获得上传进度。
 * 返回新视频的 id（兼容后端返回 {video_id} 或 {id}）。
 */
export function uploadVideo(file: File, onProgress: (fraction: number) => void): UploadHandle {
  const xhr = new XMLHttpRequest()
  const promise = new Promise<string>((resolve, reject) => {
    xhr.open('POST', '/api/videos')
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) onProgress(e.loaded / e.total)
    }
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          const data = JSON.parse(xhr.responseText) as { video_id?: string; id?: string }
          const id = data.video_id ?? data.id
          if (!id) throw new Error()
          resolve(id)
        } catch {
          reject(new ApiError(tr("上传成功，但后端返回格式无法识别"), xhr.status))
        }
      } else {
        reject(parseErrorBody(xhr.responseText, xhr.status, xhr.statusText))
      }
    }
    xhr.onerror = () => reject(new ApiError(tr("上传失败：无法连接后端服务"), 0))
    xhr.onabort = () => reject(new ApiError(tr("上传已取消"), 0))
    const form = new FormData()
    form.append('file', file, file.name)
    xhr.send(form)
  })
  return { promise, abort: () => xhr.abort() }
}

// ---------- 处理流程 ----------

export const getSendPlan = (id: string) => request<SendPlan>(`${base(id)}/send-plan`)

export const confirmSend = (id: string, remember: boolean) =>
  request<VideoDetail>(`${base(id)}/confirm`, jsonBody({ remember }))

/** 为该视频运行读幻灯片 → 整理证据 → 生成大纲；会先停在发送确认 */
export const runOutline = (id: string) => request<VideoDetail>(`${base(id)}/outline/run`, { method: 'POST' })

/** 从第一个未完成/失败的阶段重试；传 fromStage 时从该阶段起重跑（用于 stale_stages） */
export const retryVideo = (id: string, fromStage?: StageName) =>
  request<VideoDetail>(`${base(id)}/retry`, jsonBody(fromStage ? { from_stage: fromStage } : {}))


/** 大纲未生成（404）时返回 null；兼容旧格式（没有 admin、speech_categories） */
export async function getOutline(id: string): Promise<Outline | null> {
  try {
    const o = await request<Partial<Outline>>(`${base(id)}/outline`)
    const admin =
      o?.admin && Array.isArray(o.admin.items) ? { title: o.admin.title || tr("课程事务"), items: o.admin.items } : null
    return {
      chapters: o?.chapters ?? [],
      revision: o?.revision,
      admin,
      speech_categories: o?.speech_categories && !Array.isArray(o.speech_categories) ? o.speech_categories : {},
      warnings: o?.warnings ?? [],
    }
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) return null
    throw e
  }
}

/** 幻灯片记录（可选接口）；不可用时返回 null */
export async function getSlides(id: string): Promise<Slide[] | null> {
  try {
    return await request<Slide[]>(`${base(id)}/slides`)
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) return null
    throw e
  }
}

/**
 * 订阅处理进度（SSE）。连接后后端先推 snapshot。
 * 同时兼容无名事件（onmessage）与具名事件（event: snapshot/stage/video）。
 */
export function subscribeEvents(
  id: string,
  onEvent: (e: ProgressEvent) => void,
  onConnection?: (state: 'open' | 'reconnecting') => void,
): () => void {
  const es = new EventSource(`${base(id)}/events`)
  const handle = (msg: MessageEvent) => {
    if (typeof msg.data !== 'string' || !msg.data.trim()) return
    try {
      onEvent(JSON.parse(msg.data) as ProgressEvent)
    } catch {
      /* 忽略无法解析的事件（如心跳） */
    }
  }
  es.onmessage = handle
  for (const name of ['snapshot', 'stage', 'video']) es.addEventListener(name, handle as EventListener)
  es.onopen = () => onConnection?.('open')
  es.onerror = () => onConnection?.('reconnecting')
  return () => es.close()
}

// ---------- 问答 ----------

export const getChat = (id: string) => request<ChatMessage[]>(`${base(id)}/chat`)
export const getChatRun = (id: string, conversationId?: string, signal?: AbortSignal) =>
  request<ChatRun | null>(`${base(id)}/chat/run${conversationId ? `?conversation_id=${encodeURIComponent(conversationId)}` : ''}`, { signal })
export const getChatContext = (id: string) => request<ContextSnapshot | null>(`${base(id)}/chat/context`)

export interface ChatConversation {
  id: string
  created_at: string
  updated_at: string
  title: string | null
}
export interface ChatConversations {
  current_id: string
  conversations: ChatConversation[]
}
export const listChatConversations = (id: string) => request<ChatConversations>(`${base(id)}/chat/conversations`)
export const createChatConversation = (id: string) => request<ChatConversations>(`${base(id)}/chat/conversations`, { method: 'POST' })
export const selectChatConversation = (id: string, conversationId: string) =>
  request<ChatConversations>(`${base(id)}/chat/conversations/${encodeURIComponent(conversationId)}`, { method: 'PUT' })
export const renameChatConversation = (id: string, conversationId: string, title: string) =>
  request<ChatConversations>(`${base(id)}/chat/conversations/${encodeURIComponent(conversationId)}`, { ...jsonBody({ title }), method: 'PATCH' })
export const deleteChatConversation = (id: string) => request<ChatConversations>(`${base(id)}/chat`, { method: 'DELETE' })

export const stopChat = (id: string, runId?: string) => request<{ ok: boolean }>(`${base(id)}/chat/stop${runId ? `?run_id=${encodeURIComponent(runId)}` : ''}`, { method: 'POST' })

export interface OutlinePreview {
  proposal_id: string
  base_revision: string
  before: Outline
  after: Outline
  diff: string
}

export const commitOutline = (id: string, proposalId: string) =>
  request<Outline>(`${base(id)}/outline/commit`, jsonBody({ proposal_id: proposalId }))

export const undoOutline = (id: string, revision: string) =>
  request<Outline>(`${base(id)}/outline/undo`, jsonBody({ base_revision: revision }))

export interface CodexSettings {
  provider: 'api' | 'codex'
  model: string
  effort: string
  workspace_root: string
}

export interface CodexStatus {
  available: boolean
  version: string | null
  account: { type: string; email?: string | null; planType?: string | null } | null
  rate_limits: Record<string, unknown> | null
  workspace_mode: string
  isolation_verified: boolean
  error: string | null
}

export const getCodexSettings = () => request<CodexSettings>('/api/codex/settings')
export const saveCodexSettings = ({ provider, model, effort, workspace_root }: CodexSettings) =>
  request<CodexSettings>('/api/codex/settings', { ...jsonBody({ provider, model, effort, workspace_root }), method: 'PUT' })
export const getCodexStatus = () => request<CodexStatus>('/api/codex/status')
export const loginCodex = () => request<{ login_id: string; auth_url: string }>('/api/codex/login', { method: 'POST' })
export const cancelCodexLogin = (loginId: string) =>
  request<void>('/api/codex/login/cancel', jsonBody({ login_id: loginId }))
export const logoutCodex = () => request<void>('/api/codex/logout', { method: 'POST' })

export interface NoteTarget { name: string; revision: string }
export interface NoteDocument extends NoteTarget {
  text: string
  can_undo: boolean
  citations: Citation[]
  stale: boolean
}
/** 页面列表项：agent_new 为 Agent 新建且用户还没打开过；has_proposal 为有待审阅的 Agent 建议 */
export interface NoteItem extends NoteTarget { agent_new: boolean; has_proposal: boolean; pdf?: PdfDocument }
export interface PdfTarget { id: string; revision: string }
export interface PdfDocument extends PdfTarget { name: string; size: number; page_count: number; text_pages: number }
export const pdfFileUrl = (id: string, pdf: PdfTarget, download = false) =>
  `${base(id)}/pdfs/${pdf.id}/file?revision=${encodeURIComponent(pdf.revision)}${download ? '&download=true' : ''}`
export const listPdfs = (id: string) => request<PdfDocument[]>(`${base(id)}/pdfs`)
export interface TrashedPage { trash_id: string; name: string; deleted_at?: number }
export const listTrashedPdfs = (id: string) => request<TrashedPage[]>(`${base(id)}/pdfs/trash`)
export const listTrashedNotes = (id: string) => request<TrashedPage[]>(`${base(id)}/notes/trash`)
export const uploadPdf = (id: string, file: File) => request<PdfDocument>(`${base(id)}/pdfs?name=${encodeURIComponent(file.name)}`, { method: 'POST', body: file, headers: { 'Content-Type': 'application/pdf' } })
export const deletePdf = (id: string, fileId: string) => request<{ trash_id: string }>(`${base(id)}/pdfs/${fileId}`, { method: 'DELETE' })
export const restorePdf = (id: string, fileId: string) => request<PdfDocument>(`${base(id)}/pdfs/trash/${fileId}/restore`, { method: 'POST' })
export const pdfPage = (pdf: PdfDocument): NoteItem => ({ name: `pdf:${pdf.id}`, revision: pdf.revision, agent_new: false, has_proposal: false, pdf })
export interface NoteVersion {
  id: string
  at: string
  author: 'user' | 'agent'
  label: string | null
  revision: string
  current: boolean
}
export interface NoteVersionBody extends Omit<NoteVersion, 'current'> { text: string }
export interface NoteProposal {
  id: string
  name: string
  base_revision: string
  base_text: string
  text: string
  evidence_version: string
  created_at: string
}
const noteUrl = (id: string, name: string) => `${base(id)}/notes/${encodeURIComponent(name)}`
export const listNotes = (id: string) => request<NoteItem[]>(`${base(id)}/notes`)
export const createNote = (id: string, body: { title?: string; name?: string; text?: string } = {}) =>
  request<NoteDocument>(`${base(id)}/notes`, jsonBody(body))
export const getNote = (id: string, name: string) => request<NoteDocument>(noteUrl(id, name))
export const saveNote = (id: string, name: string, text: string, revision: string) =>
  request<NoteDocument>(noteUrl(id, name), { ...jsonBody({ text, base_revision: revision }), method: 'PUT' })
export const undoNote = (id: string, name: string, revision: string) =>
  request<NoteDocument>(`${noteUrl(id, name)}/undo`, jsonBody({ base_revision: revision }))
export const renameNote = (id: string, name: string, newName: string, revision: string) =>
  request<NoteDocument>(`${noteUrl(id, name)}/rename`, jsonBody({ new_name: newName, base_revision: revision }))
export const deleteNote = (id: string, name: string) =>
  request<{ trash_id: string; name: string }>(noteUrl(id, name), { method: 'DELETE' })
export const restoreTrashedNote = (id: string, trashId: string) =>
  request<NoteDocument>(`${base(id)}/notes/trash/${encodeURIComponent(trashId)}/restore`, { method: 'POST' })
export const markNoteSeen = (id: string, name: string) => request<{ name: string }>(`${noteUrl(id, name)}/seen`, { method: 'POST' })
export const listNoteVersions = (id: string, name: string) => request<NoteVersion[]>(`${noteUrl(id, name)}/versions`)
export const getNoteVersion = (id: string, name: string, versionId: string) =>
  request<NoteVersionBody>(`${noteUrl(id, name)}/versions/${encodeURIComponent(versionId)}`)
export const restoreNoteVersion = (id: string, name: string, versionId: string, revision: string) =>
  request<NoteDocument>(`${noteUrl(id, name)}/restore`, jsonBody({ version_id: versionId, base_revision: revision }))
export const getNoteProposal = (id: string, name: string) => request<NoteProposal | null>(`${noteUrl(id, name)}/proposal`)
export const resolveNoteProposal = (id: string, name: string, body: { proposal_id: string; text: string; base_revision: string; accepted: number; total: number }) =>
  request<NoteDocument>(`${noteUrl(id, name)}/proposal/resolve`, jsonBody(body))
export const discardNoteProposal = (id: string, name: string) => request<{ name: string }>(`${noteUrl(id, name)}/proposal`, { method: 'DELETE' })
export interface CodexModel { id: string; display_name: string; efforts: string[]; default_effort: string | null }
export interface ChatModel { model: string; effort: string }
export interface AgentInteraction {
  id: string
  method: string
  params: {
    reason?: string; command?: string; cwd?: string; grantRoot?: string; details?: unknown; permissions?: unknown; additionalPermissions?: unknown
    availableDecisions?: string[]
    questions?: { id: string; header: string; question: string; isSecret?: boolean; options?: { label: string; description: string }[] | null }[]
  }
}
export const respondToAgent = (id: string, requestId: string, decision: string, answers: Record<string, string>) =>
  request<{ ok: boolean }>(`${base(id)}/chat/respond`, jsonBody({ request_id: requestId, decision, answers }))
export const getCodexModels = () =>
  request<{ models: CodexModel[]; error: string | null }>('/api/codex/models')
export const getChatModel = (id: string) => request<ChatModel>(`${base(id)}/chat/model`)
export const saveChatModel = (id: string, selection: ChatModel) =>
  request<ChatModel>(`${base(id)}/chat/model`, { ...jsonBody(selection), method: 'PUT' })

/**
 * 提问。后端以 SSE 流式返回（POST，不能用 EventSource），这里用 fetch + ReadableStream 手动解析 data: 行。
 */
export async function askStream(
  id: string,
  question: string,
  onEvent: (e: ChatEvent) => void,
  signal?: AbortSignal,
  options: { node_id?: string; allow_outline_edit?: boolean; note_target?: NoteTarget; pdf_target?: PdfTarget; run_id?: string } = {},
): Promise<void> {
  let res: Response
  try {
    res = await fetch(`${base(id)}/chat`, {
      ...jsonBody({ question, ...options }),
      headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
      signal,
    })
  } catch (e) {
    if ((e as Error)?.name === 'AbortError') throw e
    throw new ApiError(tr("无法连接后端服务，请确认后端已启动"), 0)
  }
  if (!res.ok) throw await toApiError(res)
  await readEventStream<ChatEvent>(res, onEvent)
}

async function readEventStream<T>(res: Response, onEvent: (event: T) => void): Promise<void> {
  if (!res.body) throw new ApiError(tr("浏览器不支持流式读取"), 0)

  const reader = res.body.pipeThrough(new TextDecoderStream()).getReader()
  let buffer = ''
  const flushBlock = (block: string) => {
    const dataLines: string[] = []
    for (const rawLine of block.split(/\r?\n/)) {
      if (rawLine.startsWith('data:')) {
        dataLines.push(rawLine.slice(5).replace(/^ /, ''))
      }
    }
    if (!dataLines.length) return
    const data = dataLines.join('\n').trim()
    if (!data || data === '[DONE]') return
    const event = JSON.parse(data) as T
    onEvent(event)
  }
  try {
    for (;;) {
      const { value, done } = await reader.read()
      if (done) break
      buffer += value
      const blocks = buffer.split(/\r?\n\r?\n/)
      buffer = blocks.pop() ?? ''
      blocks.forEach(flushBlock)
    }
    if (buffer.trim()) flushBlock(buffer)
  } finally {
    await reader.cancel().catch(() => {})
    reader.releaseLock()
  }
}

// ---------- 抽帧展示台 ----------

/** 相对坐标 [x, y, w, h]，取值 0~1 */
export type Roi = [number, number, number, number]

export interface KeyframeSlide {
  id: string
  image: string
  appearances: [number, number][]
  /** 每次出现的截图，与 appearances 等长（0001 §3.7.1）；旧版本抽帧没有 */
  appearance_images?: string[]
  similar_prev?: { id: string; score: number } | null
}

export interface KeyframeRun {
  start: number
  end: number
  slide: string
  /** new：新幻灯片；merged：与上一张合并（追加内容）；revisit：翻回旧页 */
  kind: 'new' | 'merged' | 'revisit'
}

export interface KeyframeDebug {
  duration_s: number
  threshold: number
  drift_threshold: number
  roi: Roi | null
  /** [t, 与上一采样的变化像素占比 %, 与本段首帧的变化占比 %] */
  trace: [number, number, number][]
  /** 因停留不足而被丢弃的画面段 */
  short_runs: [number, number][]
  runs: KeyframeRun[]
  /** 内容框：ROI 裁剪图中的相对坐标 [x, y, w, h]（0001 §3.2）；旧版本抽帧没有 */
  content_box?: Roi
  content_box_px?: [number, number, number, number] | null
  content_box_size?: [number, number] | null
  /** 没找到明显黑边，退回整个 ROI */
  content_box_fallback?: boolean
  /** 丢弃的空白段 [start, end, 内容像素 %]（0001 §3.5） */
  blank_runs?: [number, number, number][]
}

export interface KeyframesResult {
  slides: KeyframeSlide[]
  /** 旧版本抽帧没有调试数据 */
  debug: KeyframeDebug | null
  params: Record<string, unknown> | null
  /** 抽帧使用的区域（已确认的优先；旧视频为视频自己的或全局默认），null 表示整个画面 */
  roi: Roi | null
  roi_default: Roi | null
}

export const getKeyframes = (id: string) => request<KeyframesResult>(`/api/videos/${id}/keyframes`)

export interface SaveRoiOptions {
  /** 写入确认记录（默认 true）；未抽过帧时随即开始抽帧。false 只保存为预填建议 */
  confirm?: boolean
  /** 使用整个画面（忽略 roi） */
  fullFrame?: boolean
  /** 已抽过帧时按新区域重新抽帧（默认 true） */
  rerun?: boolean
}

/** 保存并确认幻灯片区域；返回视频详情 */
export const saveKeyframeRoi = (id: string, roi: Roi | null, asDefault: boolean, opts: SaveRoiOptions = {}) =>
  request<VideoDetail>(`/api/videos/${id}/keyframes/roi`, {
    ...jsonBody({
      roi: opts.fullFrame ? null : roi,
      as_default: asDefault,
      rerun: opts.rerun ?? true,
      confirm: opts.confirm ?? true,
      full_frame: opts.fullFrame ?? false,
    }),
    method: 'PUT',
  })

export const frameUrl = (id: string, t: number, w = 480) =>
  `/api/videos/${id}/frame?t=${t.toFixed(1)}&w=${w}`

// ---------- 设置：API Key ----------

export interface KeyInfo {
  has_key: boolean
  /** 打码后的末几位，如 "…abcd" */
  key_hint: string | null
  /** settings：设置页填写；env：来自 .env */
  source: 'settings' | 'env' | null
}

export interface ApiKeys {
  deepseek: KeyInfo
  deepl: KeyInfo
  google: KeyInfo
  openai: KeyInfo & { base_url: string }
}

/** 字段缺省表示不修改，"" 表示清除 */
export interface KeysUpdate {
  deepseek?: string
  deepl?: string
  google?: string
  translator?: TranslationProvider
  openai?: string
  openai_base_url?: string
}

export interface ServiceTestResult {
  ok: boolean
  message: string
  latency_ms: number
}

/** 美元 / 百万 token；long_* 为单次请求输入超过 long_threshold 时的价格；flex_* 为 Flex 档价格。
 * 修改价格时后端按整条替换：未改动的字段（包括 flex_*、long_*）要原样带上，否则会被删掉 */
export interface ModelPrice {
  input: number
  output: number
  cached_input?: number
  cache_write_multiplier?: number
  long_threshold?: number
  long_input?: number
  long_cached_input?: number
  long_output?: number
  flex_input?: number
  flex_cached_input?: number
  flex_output?: number
  flex_long_input?: number
  flex_long_cached_input?: number
  flex_long_output?: number
  note?: string
}

export type LlmPurpose = 'read_slides' | 'outline' | 'qa'

/** 处理档位：flex 半价、较慢，资源不足时自动改用标准档 */
export type ServiceTier = 'flex' | 'standard'

export interface LlmSettings {
  provider: string
  models: Record<LlmPurpose, string>
  /** 各用途的思考（推理）强度 */
  efforts: Record<LlmPurpose, string>
  effort_options: string[]
  /** 各用途的处理档位；旧后端没有 */
  service_tiers?: Record<LlmPurpose, ServiceTier>
  service_tier_options?: ServiceTier[]
  outline_enabled: boolean
  /** 大纲与问答的输出语言，如 zh-CN / en；旧后端没有 */
  output_language?: string
  /** 语言代码 → 显示名 */
  output_language_options?: Record<string, string>
  usd_to_sgd: number
  prices: Record<string, ModelPrice>
  default_prices: Record<string, ModelPrice>
  /** 用户自己填写或修改过价格的模型 */
  custom_prices: string[]
  prices_as_of: string
  flex_prices_as_of?: string
  purposes: { id: LlmPurpose; label: string }[]
}

export interface SettingsView {
  keys: ApiKeys
  translator: TranslationProvider
  ollama: { model: string; base_url: string }
  llm: LlmSettings
}

export interface LlmUpdate {
  models?: Partial<Record<LlmPurpose, string>>
  efforts?: Partial<Record<LlmPurpose, string>>
  service_tiers?: Partial<Record<LlmPurpose, ServiceTier>>
  outline_enabled?: boolean
  output_language?: string
  usd_to_sgd?: number
  /** null 表示恢复默认价格 */
  prices?: Record<string, ModelPrice | null>
}

export const getSettings = () => request<SettingsView>('/api/settings')

export interface McpStatus {
  state: 'missing_dependency' | 'disabled' | 'starting' | 'ready' | 'error'
  token_configured: boolean
  endpoint: string
  tools: { name: string; description: string }[]
  library_readable: boolean | null
  protocol_version: string | null
  error_code: 'timeout' | 'protocol_failed' | 'library_unreadable' | null
  checked_at: string
  latency_ms: number
}

export const getMcpStatus = () => request<McpStatus>('/api/mcp/status')

export const saveSettings = (body: KeysUpdate) =>
  request<SettingsView>('/api/settings', { ...jsonBody(body), method: 'PUT' })

export const saveLlmSettings = (body: LlmUpdate) =>
  request<SettingsView>('/api/settings/llm', { ...jsonBody(body), method: 'PUT' })

export const listModels = () => request<{ models: string[]; error: string | null }>('/api/settings/models')

export type TranslationProvider = 'deepl' | 'google' | 'llm' | 'ollama'

export const testService = (service: 'deepl' | 'google' | 'openai' | 'ollama' | 'deepseek') =>
  request<ServiceTestResult>('/api/settings/test', jsonBody({ service }))

// ---------- 格式化 ----------

/** 秒 → mm:ss 或 h:mm:ss */
export function formatTime(sec: number | null | undefined): string {
  if (sec == null || !Number.isFinite(sec) || sec < 0) return '--:--'
  const s = Math.floor(sec)
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const r = s % 60
  const pad = (n: number) => String(n).padStart(2, '0')
  return h > 0 ? `${h}:${pad(m)}:${pad(r)}` : `${pad(m)}:${pad(r)}`
}

export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

export function errorText(e: unknown): string {
  if (e instanceof Error) return systemText(e.message)
  return systemText(String(e))
}
