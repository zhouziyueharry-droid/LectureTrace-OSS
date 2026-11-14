import { systemText, tr, useLocale } from '../i18n.ts'
import { lazy, Suspense, useCallback, useEffect, useRef, useState, type CSSProperties, type ReactNode } from 'react'
import { Link, useBlocker, useParams } from 'react-router-dom'
import { createNote, deleteNote, errorText, formatTime, getNote, getOutline, listNotes, markNoteSeen, restoreTrashedNote, retryVideo, runOutline, stageLabel, type NoteItem, type NoteTarget, type Outline, type Roi, type StageName } from '../api.ts'
import { useLibrary } from '../library.tsx'
import ChatPanel from '../components/ChatPanel.tsx'
import OutlinePanel from '../components/OutlinePanel.tsx'
import Player from '../components/Player.tsx'
import RoiEditor from '../components/RoiEditor.tsx'
import SendPlanDialog from '../components/SendPlanDialog.tsx'
import StagePanel, { stageSettled } from '../components/StagePanel.tsx'
import ThemeToggle from '../components/ThemeToggle.tsx'
import Splitter from '../components/Splitter.tsx'
import FloatingFrame from '../components/FloatingFrame.tsx'
import { CHAT_W, clamp, OUTLINE_W, resolveLayout, SPLIT_W, VIDEO_H_MIN, useLayoutPrefs } from '../useLayoutPrefs.ts'
import { findStage, useVideoProgress } from '../useVideoProgress.ts'
import type { OutlineQuote } from '../outlineQuote.ts'
import NoteDoc from '../notes/NoteDoc.tsx'
import { OUTLINE_PAGE, PageTree, pageTitle, type RowAction } from '../notes/PageList.tsx'
import type { AgentAsk } from '../notes/BlockEditor.tsx'
import { outlineToMarkdown } from '../notes/outlineMarkdown.ts'
import { listPdfs, uploadPdf, deletePdf, restorePdf, pdfPage, pdfFileUrl, type PdfDocument } from '../api.ts'
import type { Citation } from '../api.ts'
import { PdfNavigation } from '../components/CitedMarkdown.tsx'
import EditorWorkspace from '../notes/EditorWorkspace.tsx'
import { initialEditors, openEditor, closeEditor, splitEditor, mergeEditors, renameEditor } from '../notes/editorLayout.ts'
const PdfDoc = lazy(() => import('../notes/PdfDoc.tsx'))
import type { PdfJump, PdfLeaveGuard } from '../notes/PdfDoc.tsx'

const isFullFrame = (r: Roi | null | undefined) => !r || (r[0] === 0 && r[1] === 0 && r[2] === 1 && r[3] === 1)

/** 以 id 为 key 重新挂载，切换视频时状态全部重置 */
export default function VideoPageRoute() {
  useLocale()
  const { id = '' } = useParams()
  return <VideoPage key={id} id={id} />
}

function VideoPage({ id }: { id: string }) {
  useLocale()
  const { video, loadError, notFound, live, conn, refresh } = useVideoProgress(id)
  const { courses, videos: allVideos } = useLibrary()
  const videoRef = useRef<HTMLVideoElement>(null)
  const [currentTime, setCurrentTime] = useState(0)
  const layout = useLayoutPrefs()
  // 网格的实际尺寸：决定 Agent 放右侧还是视频下方、各栏能拖多宽、视频画面多大
  const [grid, setGrid] = useState<HTMLElement | null>(null)
  const [gridSize, setGridSize] = useState({ w: 0, h: 0 })
  const fit = resolveLayout(layout, gridSize.w || window.innerWidth - 28)
  const stacked = fit.arrangement === 'stack'
  // 初次进入窄屏或从宽屏收窄时收起；仍可通过顶部按钮主动展开。
  useEffect(() => {
    if (stacked) layout.closeChat()
  }, [stacked, layout.closeChat])
  useEffect(() => {
    if (!grid) return
    const observer = new ResizeObserver(() => setGridSize({ w: grid.clientWidth, h: grid.clientHeight }))
    observer.observe(grid)
    return () => observer.disconnect()
  }, [grid])

  const [outline, setOutline] = useState<Outline | null>(null)
  const [outlineLoading, setOutlineLoading] = useState(false)
  const [outlineError, setOutlineError] = useState<string | null>(null)
  const [reference, setReference] = useState<OutlineQuote | null>(null)
  const clearReference = useCallback(() => setReference(null), [])
  // ----- 页面：大纲 + 笔记 -----
  const pagesKey = `lt.pages.${id}`
  const [editors, setEditors] = useState(() => {
    try {
      const old = JSON.parse(sessionStorage.getItem(pagesKey) ?? '{}')
      const tabs = Array.isArray(old.tabs) ? old.tabs.filter((n: unknown) => typeof n === 'string') : []
      return initialEditors([OUTLINE_PAGE, ...tabs], typeof old.page === 'string' ? old.page : OUTLINE_PAGE)
    } catch { return initialEditors() }
  })
  const page = editors.groups.find(g => g.id === editors.focused)?.active ?? ''
  const openTabs = editors.opened.filter(n => n !== OUTLINE_PAGE)
  const setPage = useCallback((name: string) => setEditors(old => openEditor(old, name)), [])
  const focusGroup = useCallback((group: number) => setEditors(old => old.focused === group ? old : { ...old, focused: group }), [])
  const [notes, setNotes] = useState<NoteItem[]>([])
  const [filesOpen, setFilesOpen] = useState(true)
  const workspace = useRef<HTMLDivElement>(null)
  const closeFiles = () => {
    setFilesOpen(false)
    document.getElementById('page-files-toggle')?.focus()
  }
  const closeNarrowFiles = () => {
    if (filesOpen && workspace.current && workspace.current.clientWidth < 680) closeFiles()
  }
  useEffect(() => { closeNarrowFiles() }, [page])
  const [pdfWindows, setPdfWindows] = useState<{ pdf: PdfDocument; jump?: PdfJump }[]>([])
  const focusedPdf = page.startsWith('pdf:') ? page.slice(4) : null
  const activePdf = pdfWindows.find((w) => w.pdf.id === focusedPdf)?.pdf
  const pdfInput = useRef<HTMLInputElement>(null)
  const [importingPdf, setImportingPdf] = useState(false)
  const pdfLeave = useRef(new Map<string, PdfLeaveGuard>())
  const registerPdfLeave = useCallback((pdfId: string, guard: PdfLeaveGuard | null) => {
    if (guard) pdfLeave.current.set(pdfId, guard)
    else pdfLeave.current.delete(pdfId)
  }, [])
  const blocker = useBlocker(() => [...pdfLeave.current.values()].some((guard) => guard.shouldBlock()))
  useEffect(() => {
    if (blocker.state === 'blocked') {
      if ([...pdfLeave.current.values()].every((guard) => guard.canLeave())) blocker.proceed()
      else blocker.reset()
    }
  }, [blocker])
  const openPdf = useCallback((pdf: PdfDocument, targetPage?: number) => {
    setPdfWindows((windows) => {
      const existing = windows.find((w) => w.pdf.id === pdf.id)
      // Do not reorder DOM nodes: moving the SDK custom element disconnects its editor.
      const opened = { pdf: existing?.pdf ?? pdf,
        jump: targetPage ? { page: targetPage, request: (existing?.jump?.request ?? 0) + 1 } : existing?.jump }
      return existing ? windows.map((w) => w === existing ? opened : w) : [...windows, opened]
    })
    setEditors(old => openEditor(old, `pdf:${pdf.id}`, targetPage !== undefined))
  }, [])
  const savedPdf = useCallback((pdf: PdfDocument) => {
    const item = pdfPage(pdf)
    setNotes((old) => [...old, item])
    openPdf(pdf)
  }, [openPdf])
  const [notesLoaded, setNotesLoaded] = useState(false)
  const [notesError, setNotesError] = useState<string | null>(null)
  const [notesRefresh, setNotesRefresh] = useState(0)
  const refreshNotes = useCallback(() => setNotesRefresh((v) => v + 1), [])
  const [attachNote, setAttachNote] = useState(true)
  const [agentRequest, setAgentRequest] = useState<{ id: number; question: string; send: boolean } | null>(null)
  const [titleFocus, setTitleFocus] = useState<string | null>(null)
  const [toast, setToast] = useState<{ text: string; undo?: () => void } | null>(null)
  const noteFlush = useRef(new Map<string, () => Promise<NoteTarget | null>>())
  const registerFlush = useCallback((name: string, fn: (() => Promise<NoteTarget | null>) | null) => {
    if (fn) noteFlush.current.set(name, fn)
    else noteFlush.current.delete(name)
  }, [])
  const resolveNoteTarget = useCallback(async () => noteFlush.current.get(page)?.() ?? null, [page])
  const seenAgentPages = useRef(new Set<string>())
  const openPdfCitation = useCallback((citation: Citation) => {
    const pdf = notes.find((n) => n.pdf?.id === citation.pdf_id)?.pdf
    const revision = citation.revision ?? citation.id.split(':')[2]
    const opened = pdfWindows.find((w) => w.pdf.id === citation.pdf_id)
    if (!pdf || pdf.revision !== revision || (opened && opened.pdf.revision !== revision) || !Number.isInteger(citation.page) || citation.page! < 1 || citation.page! > pdf.page_count) {
      setToast({ text: tr("无法定位 PDF：原件已删除、版本已变化或页码无效。") })
      return
    }
    openPdf(pdf, citation.page)
  }, [notes, openPdf, pdfWindows])
  const hideVideo = () => {
    videoRef.current?.pause()
    layout.setVideoHidden(true)
    document.getElementById('video-visibility')?.focus()
  }

  const [stagesOpen, setStagesOpen] = useState(false)
  const [planOpen, setPlanOpen] = useState(false)
  const [planDismissed, setPlanDismissed] = useState(false)
  const [retrying, setRetrying] = useState(false)
  const [retryError, setRetryError] = useState<string | null>(null)
  // 幻灯片区域：新视频的确认卡片可以先收起；流程区的"修改区域"打开弹窗
  const [roiCardHidden, setRoiCardHidden] = useState(false)
  const [roiEditOpen, setRoiEditOpen] = useState(false)

  const translate = findStage(video, 'translate')
  const outlineStage = findStage(video, 'outline')
  const evidenceStage = findStage(video, 'evidence')

  // ----- 字幕版本：translate 阶段变为 done 时刷新 track -----
  const [subsVersion, setSubsVersion] = useState<string | null>(null)
  const prevTranslate = useRef<string | undefined>(undefined)
  useEffect(() => {
    const status = translate?.status
    const prev = prevTranslate.current
    prevTranslate.current = status
    if (status === 'done') {
      if (prev === undefined) setSubsVersion(translate?.finished_at || String(translate?.attempt ?? 1))
      else if (prev !== 'done') setSubsVersion(String(Date.now()))
    }
  }, [translate?.status, translate?.finished_at, translate?.attempt])
  // 字幕在 translate 重新运行期间仍保留旧版本；只有从未生成过时为 null

  // ----- 大纲：outline 阶段 done 时加载 -----
  const prevOutline = useRef<string | undefined>(undefined)
  const loadOutline = useCallback(async () => {
    setOutlineLoading(true)
    setOutlineError(null)
    try {
      setOutline(await getOutline(id))
    } catch (e) {
      setOutlineError(errorText(e))
    } finally {
      setOutlineLoading(false)
    }
  }, [id])

  useEffect(() => {
    const status = outlineStage?.status
    const prev = prevOutline.current
    prevOutline.current = status
    if (!video) return
    // 首次进入页面总是尝试加载一次（可能有旧版本大纲）；之后在 outline 变为 done 时重新加载
    if (prev === undefined || (status === 'done' && prev !== 'done')) void loadOutline()
  }, [outlineStage?.status, video, loadOutline])

  // ----- 等待确认时自动弹出发送清单 -----
  useEffect(() => {
    if (video?.status === 'awaiting_confirm' && !planDismissed) setPlanOpen(true)
    if (video && video.status !== 'awaiting_confirm') setPlanOpen(false)
  }, [video?.status, planDismissed, video])

  useEffect(() => {
    if (video?.title) document.title = `${video.title} - LectureTrace`
  }, [video?.title])

  const seek = useCallback((t: number) => {
    const v = videoRef.current
    if (!v || !Number.isFinite(t)) return
    layout.setVideoHidden(false)
    v.currentTime = Math.max(0, t)
    void v.play().catch(() => {
      /* 浏览器可能阻止自动播放，忽略 */
    })
    setCurrentTime(t)
  }, [layout.setVideoHidden])

  const onTime = useCallback((t: number) => {
    setCurrentTime((prev) => (Math.abs(prev - t) >= 0.5 ? t : prev))
  }, [])

  // ----- 页面列表、页签与页面管理 -----
  useEffect(() => {
    let active = true
    setNotesLoaded(false)
    setNotesError(null)
    Promise.all([listNotes(id), listPdfs(id)]).then(([list, pdfs]) => { if (active) { setNotes([...list, ...pdfs.map(pdfPage)]); setNotesError(null); setNotesLoaded(true) } },
      (e) => { if (active) { setNotesError(errorText(e)); setNotesLoaded(true) } })
    return () => { active = false }
  }, [id, notesRefresh])

  useEffect(() => {
    if (!notesLoaded || notesError) return
    const names = new Set(notes.map((n) => n.name))
    const newAgentPages = notes.filter(item => item.agent_new && !seenAgentPages.current.has(item.name))
    setEditors(old => {
      let result = old
      for (const name of old.opened) if (name !== OUTLINE_PAGE && !names.has(name)) result = closeEditor(result, name)
      for (const item of newAgentPages) {
        const focused = result.focused
        const active = result.groups.find(g => g.id === focused)!.active
        result = openEditor(result, item.name)
        result = { ...result, focused, groups: result.groups.map(g => g.id === focused ? { ...g, active } : g) }
      }
      return result
    })
    for (const item of notes) if (item.agent_new) seenAgentPages.current.add(item.name)
    // Restore PDF tabs lazily after their metadata has loaded.
    setPdfWindows(old => {
      const missing = notes.filter(n => n.pdf && editors.opened.includes(n.name) && !old.some(w => w.pdf.id === n.pdf!.id))
      return missing.length ? [...old, ...missing.map(n => ({ pdf: n.pdf! }))] : old
    })
  }, [notes, notesLoaded, notesError])

  useEffect(() => {
    try { sessionStorage.setItem(pagesKey, JSON.stringify({ page, tabs: openTabs })) } catch { /* 存储不可用 */ }
  }, [pagesKey, page, openTabs])
  useEffect(() => { setAttachNote(true) }, [page, focusedPdf])
  useEffect(() => {
    if (!toast) return
    const t = window.setTimeout(() => setToast(null), 9000)
    return () => window.clearTimeout(t)
  }, [toast])

  const openPage = useCallback((name: string) => {
    const pdf = notes.find((n) => n.name === name)?.pdf
    if (pdf) { openPdf(pdf); return }
    if (name !== OUTLINE_PAGE) {
      if (notes.find((n) => n.name === name)?.agent_new) void markNoteSeen(id, name).then(refreshNotes, () => {})
    }
    setPage(name)
  }, [id, notes, refreshNotes, openPdf])
  const closeTab = useCallback((name: string) => {
    if (name.startsWith('pdf:')) {
      const pdfId = name.slice(4)
      if (pdfLeave.current.get(pdfId)?.canLeave() === false) return
      setPdfWindows((windows) => windows.filter((w) => w.pdf.id !== pdfId))
      document.getElementById('page-files-toggle')?.focus()
    }
    setEditors(old => closeEditor(old, name))
  }, [])
  const focusTitle = useCallback((name: string) => {
    setTitleFocus(name)
    window.setTimeout(() => setTitleFocus(null), 400)
  }, [])
  const newPage = useCallback(async (title = tr("无标题"), text = '') => {
    try {
      const note = await createNote(id, { title, text })
      refreshNotes()
      setPage(note.name)
      if (!text) focusTitle(note.name)
    } catch (e) { setToast({ text: tr("新建页面失败：{0}", errorText(e)) }) }
  }, [id, refreshNotes, focusTitle])
  const renamed = useCallback((oldName: string, newName: string) => {
    setEditors(old => renameEditor(old, oldName, newName))
    refreshNotes()
  }, [refreshNotes])
  const removePage = useCallback(async (name: string) => {
    if (name.startsWith('pdf:') && pdfLeave.current.get(name.slice(4))?.canLeave() === false) return
    try {
      if (name.startsWith('pdf:')) {
        const pdfId = name.slice(4)
        await deletePdf(id, pdfId)
        setPdfWindows((windows) => windows.filter((w) => w.pdf.id !== pdfId))
        setEditors(old => closeEditor(old, name))
        refreshNotes()
        setToast({ text: tr("PDF 已移到回收站"), undo: () => { void restorePdf(id, pdfId).then((pdf) => {
          setNotes((old) => [...old.filter((n) => n.name !== name), pdfPage(pdf)])
          openPdf(pdf)
          setToast(null)
        }, (e) => setToast({ text: tr("恢复失败：{0}", errorText(e)) })) } })
        return
      }
      const { trash_id } = await deleteNote(id, name)
      closeTab(name)
      refreshNotes()
      setToast({
        text: tr("已移到回收站：「{0}」", pageTitle(name)),
        undo: () => { void restoreTrashedNote(id, trash_id).then((note) => { refreshNotes(); openPage(note.name); setToast(null) }, (e) => setToast({ text: tr("恢复失败：{0}", errorText(e)) })) },
      })
    } catch (e) { setToast({ text: tr("删除失败：{0}", errorText(e)) }) }
  }, [id, closeTab, refreshNotes, openPage, openPdf])
  const duplicatePage = useCallback(async (name: string) => {
    try {
      const note = await getNote(id, name)
      await newPage(tr("{0} 副本", pageTitle(name)), note.text || '\n')
    } catch (e) { setToast({ text: tr("创建副本失败：{0}", errorText(e)) }) }
  }, [id, newPage])
  const exportPage = useCallback((name: string, text: string) => {
    const url = URL.createObjectURL(new Blob([text], { type: 'text/markdown;charset=utf-8' }))
    const a = document.createElement('a')
    a.href = url
    a.download = name
    a.click()
    window.setTimeout(() => URL.revokeObjectURL(url), 1000)
  }, [])
  const rowAction = useCallback((name: string, action: RowAction) => {
    const pdf = notes.find((n) => n.name === name)?.pdf
    if (pdf) {
      if (action === 'delete') void removePage(name)
      else if (action === 'export') { const a = document.createElement('a'); a.href = pdfFileUrl(id, pdf, true); a.click() }
      return
    }
    if (action === 'rename') { openPage(name); focusTitle(name) }
    else if (action === 'duplicate') void duplicatePage(name)
    else if (action === 'delete') void removePage(name)
    else void getNote(id, name).then((note) => exportPage(name, note.text), (e) => setToast({ text: tr("导出失败：{0}", errorText(e)) }))
  }, [id, notes, openPage, focusTitle, duplicatePage, removePage, exportPage])
  const importPdf = async (file: File) => {
    setImportingPdf(true)
    try {
      const pdf = await uploadPdf(id, file)
      const item = pdfPage(pdf)
      setNotes((old) => [...old, item])
      openPdf(pdf)
      setToast({ text: tr("已导入「{0}」· {1} 页", pdf.name, pdf.page_count) })
    } catch (e) { setToast({ text: tr("导入 PDF 失败：{0}", errorText(e)) }) }
    finally { setImportingPdf(false) }
  }
  const copyOutlineToNote = useCallback(() => {
    if (outline) void newPage(tr("大纲（可编辑）"), outlineToMarkdown(outline))
  }, [outline, newPage])

  const getTime = useCallback(() => videoRef.current?.currentTime ?? 0, [])
  const askFromPage = useCallback((ask: AgentAsk) => {
    if (page === OUTLINE_PAGE) return
    const title = pageTitle(page)
    const quote = (text: string) => text.split('\n').map((line) => `> ${line}`).join('\n')
    let question: string
    if (!ask.send) question = tr("关于笔记「{0}」里的这一段：\n{1}\n", title, quote(ask.quote))
    else if (ask.where === 'insert') {
      question = tr("请在笔记「{0}」里{1}新增一段内容，只新增，已有内容逐字保留，用 write_note 以修改建议提交整页新正文。{2}\n\n要求：{3}", title, ask.before ? tr("下面这一段之后") : tr("合适的位置"), ask.before ? tr("\n\n上文：\n{0}", quote(ask.before)) : '', ask.instruction)
    } else {
      question = tr("请处理笔记「{0}」里的这一段，只改动这一段，其余内容逐字保留，用 write_note 以修改建议提交整页新正文。\n\n原文：\n{1}\n\n要求：{2}", title, quote(ask.quote), ask.instruction)
    }
    setAgentRequest({ id: Date.now(), question, send: ask.send })
    layout.openChat()
  }, [page, layout])

  const doRetry = async (fromStage?: StageName) => {
    setRetrying(true)
    setRetryError(null)
    try {
      await retryVideo(id, fromStage)
      setPlanDismissed(false)
      // 不直接用接口返回的快照：后端可能已经往前走了（例如瞬间到了等待确认），
      // 旧快照会覆盖实时推送的新状态；重新取一次最新状态，之后靠推送更新
      void refresh()
    } catch (e) {
      setRetryError(errorText(e))
    } finally {
      setRetrying(false)
    }
  }

  const doRunOutline = async () => {
    setRetrying(true)
    setRetryError(null)
    try {
      await runOutline(id)
      setPlanDismissed(false)
      void refresh()
    } catch (e) {
      setRetryError(errorText(e))
    } finally {
      setRetrying(false)
    }
  }

  if (notFound) {
    return (
      <div className="page-center">
        <p>{tr("视频不存在或已被删除。")}</p>
        <Link to="/">{tr("返回首页")}</Link>
      </div>
    )
  }

  if (!video) {
    return (
      <div className="page-center">
        {loadError ? (
          <>
            <p className="error-text">{tr("加载失败：")}{systemText(loadError)}</p>
            <button className="btn" onClick={() => void refresh()}>{tr("重新加载")}</button>
            <Link to="/">{tr("返回首页")}</Link>
          </>
        ) : (
          <p className="muted">{tr("加载中…")}</p>
        )}
      </div>
    )
  }

  const courseTitle = courses?.find((c) => c.id === video.course_id)?.title
  const siblings = video.course_id
    ? (allVideos ?? []).filter((v) => v.course_id === video.course_id).sort((a, b) => a.created_at.localeCompare(b.created_at))
    : []
  const pos = siblings.findIndex((v) => v.id === id)
  const prev = pos > 0 ? siblings[pos - 1] : null
  const next = pos >= 0 && pos < siblings.length - 1 ? siblings[pos + 1] : null
  const settled = stageSettled(video) || video.status === 'done'
  const roiPending = video.status === 'awaiting_roi' || video.roi_confirmed === false
  const kfStatus = findStage(video, 'keyframes')?.status
  // 截图默认取 1 分钟处（开头常是片头或空白），视频较短时取中间
  const roiShotTime = Math.max(0, Math.min(60, (video.duration_s ?? 120) / 2))
  const onRoiSaved = () => {
    setRoiEditOpen(false)
    setPlanDismissed(false)
    void refresh()
  }

  // ----- 各种占位提示 -----
  const running = video.stages.find((s) => s.status === 'running')
  const currentStepText = running ? tr("当前：{0}", stageLabel(running.name)) : ''
  const subsHint =
    translate?.status === 'failed'
      ? tr("字幕翻译失败，见上方状态")
      : video.status === 'awaiting_confirm'
        ? tr("确认发送后开始翻译字幕")
        : translate?.status === 'running'
          ? tr("正在翻译字幕…")
          : tr("字幕尚未生成")

  let outlinePlaceholder: ReactNode
  if (outlineStage?.status === 'skipped') {
    outlinePlaceholder = (
      <>
        <p className="muted">{tr("这个视频还没有生成大纲。")}</p>
        <p className="muted small">{tr("点上方的\"运行大纲生成\"（会先显示费用预估），或到设置页开启\"自动生成大纲\"。也可以先到")}{' '}
          <Link to={`/videos/${id}/keyframes`}>{tr("抽帧展示台")}</Link>{tr("检查切出的幻灯片。")}</p>
      </>
    )
  } else if (outlineStage?.status === 'failed') {
    outlinePlaceholder = <p className="error-text small">{tr("大纲生成失败：")}{systemText(outlineStage.error || tr("未知原因"))}</p>
  } else if (video.status === 'awaiting_confirm') {
    outlinePlaceholder = <p className="muted">{tr("确认发送清单后才会生成大纲。")}</p>
  } else if (outlineStage?.status === 'done') {
    outlinePlaceholder = <p className="muted">{tr("大纲暂不可用。")}</p>
  } else if (video.status === 'failed') {
    outlinePlaceholder = <p className="muted">{tr("处理中断，重试后继续生成大纲。")}</p>
  } else {
    outlinePlaceholder = (
      <>
        <p className="muted">{tr("大纲将在处理完成后显示。")}</p>
        {currentStepText && <p className="muted small">{currentStepText}</p>}
        {subsVersion && <p className="muted small">{tr("字幕已就绪，可以先观看视频。")}</p>}
      </>
    )
  }

  // 大纲阶段的参数（输出语言、模型）变化后，后端把 outline 列入 stale_stages（交接 0001 §10.5）
  // 只有 outline 本身过期时是设置变了；上游阶段也过期时，要从上游重跑（流程区的"重新处理"）
  const staleStages = video.stale_stages ?? []
  const outlineNeedsRegen: 'settings' | 'upstream' | undefined =
    outlineStage?.status === 'done' && staleStages.includes('outline')
      ? staleStages.some((n) => n !== 'outline' && n !== 'translate')
        ? 'upstream'
        : 'settings'
      : undefined

  const chatEnabled = evidenceStage?.status === 'done'
  const chatHint =
    evidenceStage?.status === 'skipped'
      ? tr("Agent 暂未启用。")
      : video.status === 'awaiting_confirm'
      ? tr("确认发送清单并完成处理后才能向 Agent 提问。")
      : tr("Agent 在\"整理证据\"阶段完成后可用。")

  // 默认左列上视频、下页面区，右列 Agent；仍支持把 Agent 停到视频下方。
  // 视频浮成小窗时网格里只剩页面区（占满）和 Agent
  const videoOut = fit.floating || layout.videoHidden
  const gridCols = !videoOut && fit.chatPlace === 'below'
    ? `${fit.outlineW}px ${SPLIT_W}px minmax(0, 1fr)`
    : `minmax(0, 1fr)${fit.chatPlace === 'side' ? ` ${SPLIT_W}px ${fit.chatW}px` : ''}`
  const gridStyle = (stacked ? {} : { '--vp-cols': gridCols, ...(gridSize.h ? { '--vp-h': `${gridSize.h}px` } : {}) }) as CSSProperties
  // Agent 在视频下方时，视频高度默认占列高的 55%，并给 Agent 至少留 240px
  const docked = !stacked && !videoOut && fit.chatPlace === 'below'
  const videoHMax = !stacked && !fit.floating && gridSize.h ? Math.max(VIDEO_H_MIN, gridSize.h - 300) : undefined
  const videoH = videoHMax != null ? clamp(layout.videoH ?? Math.round(gridSize.h * (docked ? 0.55 : 0.5)), VIDEO_H_MIN, videoHMax) : layout.videoH
  const dockButton = !stacked && !videoOut && (
    <button type="button" className="btn btn-small btn-text ag-dock-btn" disabled={fit.autoBelow}
      onClick={() => layout.setChatDock(fit.chatPlace === 'below' ? 'side' : 'below')}
      title={fit.autoBelow ? tr("窗口太窄，右侧放不下，已停到视频下方") : fit.chatPlace === 'below' ? tr("把 Agent 移到右侧") : tr("把 Agent 停到视频下方")}>
      <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden>
        <rect x="1.75" y="2.75" width="12.5" height="10.5" rx="2" />
        {fit.chatPlace === 'below' ? <path d="M10 2.75v10.5" /> : <path d="M1.75 9h12.5" />}
      </svg>
      {fit.chatPlace === 'below' ? tr("移到右侧") : tr("停到视频下方")}
    </button>
  )

  return (
    <PdfNavigation.Provider value={openPdfCitation}>
    <div className="video-page ide-page" data-arrangement={fit.arrangement}>
      <header className="vp-head">
        <button id="page-files-toggle" type="button" className="btn btn-small" aria-label={filesOpen ? tr("收起文件栏") : tr("展开文件栏")} aria-expanded={filesOpen} aria-controls="page-files" onClick={() => setFilesOpen(v => !v)}>
          <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden><rect x="2" y="2.5" width="12" height="11" rx="1"/><path d="M6 2.5v11"/></svg>{tr("文件")}</button>
        <button id="video-visibility" type="button" className="btn btn-small" aria-controls="vp-player" aria-expanded={!layout.videoHidden}
          onClick={() => layout.videoHidden ? layout.setVideoHidden(false) : hideVideo()}>{layout.videoHidden ? tr("打开视频") : tr("收起视频")}</button>
        <Link to="/" className="back-link" title={tr("回到课程库")}>{tr("课程库")}</Link>
        <span className="muted" aria-hidden>/</span>
        {video.course_id ? (
          <Link to={`/courses/${encodeURIComponent(video.course_id)}`} className="back-link">
            {courseTitle ?? tr("课程")}
          </Link>
        ) : (
          <Link to="/inbox" className="back-link">{tr("未归类")}</Link>
        )}
        <span className="muted" aria-hidden>/</span>
        <h1 className="vp-title ellipsis" title={video.filename}>
          {video.title || video.filename}
        </h1>
        {video.duration_s != null && <span className="muted mono small">{formatTime(video.duration_s)}</span>}
        {siblings.length > 1 && (
          <span className="lt-lecnav">
            {prev ? (
              <Link to={`/videos/${encodeURIComponent(prev.id)}`} className="btn btn-small" title={prev.title || prev.filename} aria-label={tr("上一讲")}>‹</Link>
            ) : (
              <span className="btn btn-small" aria-disabled="true">‹</span>
            )}
            <span className="mono small muted">{pos + 1} / {siblings.length}</span>
            {next ? (
              <Link to={`/videos/${encodeURIComponent(next.id)}`} className="btn btn-small" title={next.title || next.filename} aria-label={tr("下一讲")}>›</Link>
            ) : (
              <span className="btn btn-small" aria-disabled="true">›</span>
            )}
          </span>
        )}
        <span className="spacer" />
        {settled && (
          <button type="button" className="stage-chip" aria-expanded={stagesOpen} onClick={() => setStagesOpen((v) => !v)} title={tr("处理已完成，点开查看各阶段、重新处理")}>
            {video.stale_stages?.length ? tr("处理详情 · 有更新") : tr("处理详情")}
          </button>
        )}
        <Link to={`/videos/${id}/keyframes`} className="btn btn-small">{tr("抽帧展示台")}</Link>
        <Link to="/settings" className="btn btn-small">{tr("设置")}</Link>
        <ThemeToggle />
        <button type="button" className={`btn btn-small vp-agent-toggle${layout.chatOpen ? ' is-on' : ''}`} aria-expanded={layout.chatOpen} aria-controls="vp-agent" onClick={layout.toggleChat} title={layout.chatOpen ? tr("收起 Agent 侧栏") : tr("呼出 Agent 侧栏")}>
          <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden><rect x="1.75" y="2.75" width="12.5" height="10.5" rx="2" /><path d="M10 2.75v10.5" /></svg>
          Agent
        </button>
      </header>

      {(!settled || stagesOpen) && <StagePanel
        startOpen={settled}
        video={video}
        live={live}
        conn={conn}
        retrying={retrying}
        retryError={systemText(retryError)}
        onRetry={(from) => void doRetry(from)}
        onRunOutline={() => void doRunOutline()}
        onOpenPlan={() => {
          setPlanDismissed(false)
          setPlanOpen(true)
        }}
        onEditRoi={() => setRoiEditOpen(true)}
      />}

      {roiPending &&
        (roiCardHidden ? (
          <div className="alert alert-warn small roi-line">
            <span>
              {video.status === 'awaiting_roi'
                ? tr("字幕已完成，框选幻灯片区域后才会开始抽帧。")
                : tr("尚未确认幻灯片区域：确认后才会开始抽帧（语音识别照常进行）。")}
            </span>
            <button className="btn btn-small btn-primary" onClick={() => setRoiCardHidden(false)}>{tr("框选幻灯片区域")}</button>
          </div>
        ) : (
          <RoiEditor
            key="roi-card"
            variant="card"
            videoId={id}
            time={roiShotTime}
            duration={video.duration_s}
            initial={isFullFrame(video.roi_suggestion) ? null : video.roi_suggestion}
            confirmLabel={tr("确认")}
            intro={
              <p className="small">
                {video.status === 'awaiting_roi'
                  ? tr("语音识别和字幕已完成。框选幻灯片所在的区域并确认后，开始提取幻灯片。")
                  : tr("确认幻灯片所在的区域后开始提取幻灯片；语音识别不受影响，照常进行。")}
                {video.roi_suggestion ? tr("已预填上次使用的区域，可直接确认或重新框选。") : ''}
              </p>
            }
            onClose={() => setRoiCardHidden(true)}
            onSaved={onRoiSaved}
          />
        ))}

      <main ref={setGrid} className="vp-grid" data-chat={fit.chatPlace} data-float={videoOut ? 'on' : undefined} style={gridStyle}>
        <input ref={pdfInput} type="file" accept=".pdf,application/pdf" hidden aria-label={tr("选择 PDF 文件")} onChange={(event) => {
          const file = event.target.files?.[0]; event.target.value = ''; if (file) void importPdf(file)
        }} />
        <FloatingFrame floating={fit.floating} hidden={layout.videoHidden} onHide={hideVideo} box={layout.floatBox} avoidRight={fit.chatPlace === 'side' ? fit.chatW + SPLIT_W + 14 : 0} onBox={layout.setFloatBox} onDock={layout.toggleFloat}>
          <Player videoId={id} videoRef={videoRef} subsVersion={subsVersion} subsHint={systemText(subsHint)} onTime={onTime} videoH={fit.floating ? null : videoH} videoHMax={videoHMax}
            splitLabel={docked ? tr("拖拽调整视频与 Agent 的高度") : tr("拖拽调整视频与页面区的高度")} onVideoH={layout.setVideoH}
            floating={fit.floating} onToggleFloat={layout.toggleFloat} onHide={hideVideo} />
        </FloatingFrame>
        <div id="vp-outline" className="vp-outline">
          <section className="panel doc-area" aria-label={tr("页面区")}>
            <div className="doc-workspace" ref={workspace} onKeyDown={(event) => {
              if (event.key === 'Escape' && filesOpen) closeFiles()
            }}>
            <PageTree videoId={id} hidden={!filesOpen} items={notes} loading={!notesLoaded} error={systemText(notesError)} onRetry={refreshNotes} page={page}
              onOpen={(name) => { openPage(name); closeNarrowFiles() }} onNew={() => void newPage()} onImport={() => pdfInput.current?.click()}
              importing={importingPdf} onAction={(name, action) => { rowAction(name, action); if (action === 'rename') closeNarrowFiles() }} />
            {filesOpen && <button type="button" className="pg-backdrop" aria-label={tr("关闭文件栏遮罩")} tabIndex={-1} onClick={closeFiles} />}
            <EditorWorkspace layout={editors} items={notes} onOpen={openPage} onClose={closeTab} onFocus={focusGroup}
              onSplit={() => setEditors(splitEditor)} onMerge={() => setEditors(mergeEditors)} onBrowse={() => setFilesOpen(true)}
              render={(name) => name === OUTLINE_PAGE ? (
                <OutlinePanel
                  embedded
                  headExtra={outline ? <button className="btn btn-small" onClick={copyOutlineToNote} title={tr("得到一份可以自由编辑的笔记副本")}>{tr("复制为笔记")}</button> : undefined}
                  outline={outline}
                  loading={outlineLoading && !outline}
                  error={systemText(outlineError)}
                  placeholder={outlinePlaceholder}
                  currentTime={currentTime}
                  onSeek={seek}
                  onReference={(quote) => { setReference(quote); layout.openChat() }}
                  needsRegen={outlineNeedsRegen}
                  onRegen={
                    outlineNeedsRegen === 'settings' && (video.status === 'done' || video.status === 'failed')
                      ? () => void doRetry('outline')
                      : undefined
                  }
                  regenBusy={retrying}
                />
              ) : name.startsWith('pdf:') ? (() => {
                const opened = pdfWindows.find(w => `pdf:${w.pdf.id}` === name)
                return opened ? <Suspense fallback={<p className="muted editor-loading" role="status">{tr("正在加载 PDF…")}</p>}>
                  <PdfDoc videoId={id} pdf={opened.pdf} jump={opened.jump} onDelete={() => void removePage(name)} onSaved={savedPdf} registerLeave={registerPdfLeave} />
                </Suspense> : <p role="status">{tr("正在读取 PDF…")}</p>
              })() : (
                <NoteDoc videoId={id} name={name} refreshKey={notesRefresh} onChanged={refreshNotes} onSeek={seek} getTime={getTime}
                  agentEnabled={chatEnabled} onAgent={askFromPage} autoFocusTitle={titleFocus === name}
                  onRenamed={renamed} onDelete={(name) => void removePage(name)} onDuplicate={(name) => void duplicatePage(name)} onExport={exportPage}
                  registerFlush={registerFlush} />
              )} />
            </div>
          </section>
        </div>
        {docked && <Splitter className="vp-split-1" direction="col" label={tr("拖拽调整页面区与视频的宽度")} controls="vp-outline" value={fit.outlineW} min={OUTLINE_W.min} max={fit.outlineMax} onStart={() => fit.outlineW} onResize={(w) => layout.setOutlineW(w, fit.outlineMax)} onReset={() => layout.setOutlineW(OUTLINE_W.def, fit.outlineMax)} />}
        {fit.chatPlace === 'side' && (
          <Splitter className="vp-split-2" direction="col" sign={-1} label={tr("拖拽调整 Agent 侧栏宽度")} controls="vp-agent" value={fit.chatW} min={CHAT_W.min} max={fit.chatMax} onStart={() => fit.chatW} onResize={(w) => layout.setChatW(w, fit.chatMax)} onReset={() => layout.setChatW(CHAT_W.def, fit.chatMax)} />
        )}
        {/* 收起或换位置时都保持挂载（只换网格区域），正在进行的回答和会话状态不丢 */}
        <div id="vp-agent" className={`vp-chat${layout.chatOpen ? '' : ' is-hidden'}`}>
          <ChatPanel videoId={id} enabled={chatEnabled} disabledHint={systemText(chatHint)} onSeek={seek} reference={reference} onClearReference={clearReference} onOutlineChanged={loadOutline} outlineRevision={outline?.revision}
            headExtra={dockButton}
            noteAttach={attachNote ? activePdf?.name ?? (page !== OUTLINE_PAGE ? pageTitle(page) : null) : null} onDetachNote={() => setAttachNote(false)} resolveNoteTarget={resolveNoteTarget}
            pdfTarget={attachNote && activePdf ? { id: activePdf.id, revision: activePdf.revision } : undefined}
            agentRequest={agentRequest} onNotesChanged={refreshNotes} />
        </div>
      </main>

      {toast && (
        <div className="toast" role="status">
          <span>{systemText(toast.text)}</span>
          {toast.undo && <button type="button" className="toast-undo" onClick={toast.undo}>{tr("撤销")}</button>}
          <button type="button" className="toast-close" aria-label={tr("关闭提示")} onClick={() => setToast(null)}>×</button>
        </div>
      )}

      {roiEditOpen && (
        <RoiEditor
          videoId={id}
          open
          time={currentTime > 0 ? currentTime : roiShotTime}
          duration={video.duration_s}
          initial={isFullFrame(video.roi) ? null : video.roi}
          confirmLabel={kfStatus === 'done' || kfStatus === 'failed' ? tr("保存并重新抽帧") : tr("确认")}
          intro={
            <p className="small">{tr("保存后只重跑提取幻灯片及其下游（切分讲解片段、读取幻灯片、整理证据、生成大纲），语音识别和字幕不重跑； 涉及外部服务的阶段开始前会先显示发送清单与费用预估。")}</p>
          }
          onClose={() => setRoiEditOpen(false)}
          onSaved={onRoiSaved}
        />
      )}

      <SendPlanDialog
        videoId={id}
        open={planOpen}
        onClose={() => {
          setPlanOpen(false)
          setPlanDismissed(true)
        }}
        onConfirmed={() => {
          setPlanOpen(false)
          setPlanDismissed(true)
          void refresh()
        }}
      />
    </div>
    </PdfNavigation.Provider>
  )
}
