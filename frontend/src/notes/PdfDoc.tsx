import { systemText, tr, useLocale, getLocale } from '../i18n.ts'
import { useCallback, useEffect, useRef, useState } from 'react'
import { PDFViewer, PdfAnnotationSubtype, type PDFViewerRef, type PluginRegistry, type AnnotationPlugin, type DocumentManagerPlugin, type ExportPlugin, type ScrollPlugin, type CommandsPlugin, type I18nPlugin } from '@embedpdf/react-pdf-viewer'
import wasmUrl from '@embedpdf/pdfium/pdfium.wasm?url'
import chineseFont from '../../node_modules/@embedpdf/fonts-sc/fonts/NotoSansHans-Regular.otf?url'
import { errorText, pdfFileUrl, uploadPdf, type PdfDocument } from '../api.ts'

export interface PdfLeaveGuard { canLeave: () => boolean; shouldBlock: () => boolean }
export interface PdfJump { page: number; request: number }

interface Props {
  videoId: string
  pdf: PdfDocument
  onDelete: () => void
  onSaved: (pdf: PdfDocument) => void
  registerLeave: (id: string, guard: PdfLeaveGuard | null) => void
  jump?: PdfJump
}

export default function PdfDoc({ videoId, pdf, onDelete, onSaved, registerLeave, jump }: Props) {
  const locale = useLocale()
  const viewer = useRef<PDFViewerRef>(null)
  const registry = useRef<PluginRegistry | null>(null)
  const cleanups = useRef<(() => void)[]>([])
  const version = useRef(0)
  const savedVersion = useRef(0)
  const busyRef = useRef(false)
  const [dirty, setDirty] = useState(false)
  const [busy, setBusy] = useState(false)
  const [ready, setReady] = useState(false)
  const [layoutReady, setLayoutReady] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [status, setStatus] = useState(tr("正在加载 PDF…"))
  const url = pdfFileUrl(videoId, pdf)

  useEffect(() => {
    registry.current?.getPlugin<I18nPlugin>('i18n')?.provides().setLocale(locale)
  }, [locale])

  const mayLeave = useCallback(() => {
    if (busyRef.current) return false
    return version.current === savedVersion.current || window.confirm(tr("PDF 批注尚未保存或导出。放弃这些修改并离开？"))
  }, [])
  useEffect(() => {
    if (layoutReady && jump) registry.current?.getPlugin<ScrollPlugin>('scroll')?.provides().forDocument(pdf.id)
      .scrollToPage({ pageNumber: jump.page, behavior: 'instant' })
  }, [layoutReady, jump, pdf.id])
  useEffect(() => {
    registerLeave(pdf.id, { canLeave: mayLeave, shouldBlock: () => busyRef.current || version.current !== savedVersion.current })
    const warn = (event: BeforeUnloadEvent) => {
      if (version.current !== savedVersion.current || busyRef.current) { event.preventDefault(); event.returnValue = '' }
    }
    window.addEventListener('beforeunload', warn)
    const theme = () => viewer.current?.container?.setTheme(document.documentElement.dataset.theme === 'dark' ? 'dark' : document.documentElement.dataset.theme === 'light' ? 'light' : 'system')
    const observer = new MutationObserver(theme)
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })
    return () => {
      registerLeave(pdf.id, null)
      window.removeEventListener('beforeunload', warn)
      observer.disconnect()
      cleanups.current.splice(0).forEach((off) => off())
    }
  }, [registerLeave, mayLeave])

  const onReady = (value: PluginRegistry) => {
    registry.current = value
    value.getPlugin<I18nPlugin>('i18n')?.provides().setLocale(getLocale())
    const scroll = value.getPlugin<ScrollPlugin>('scroll')!.provides()
    cleanups.current.push(scroll.onLayoutReady((event) => { if (event.documentId === pdf.id) setLayoutReady(true) }))
    if (scroll.forDocument(pdf.id).getLayout().virtualItems.length > 0) setLayoutReady(true)
    const documents = value.getPlugin<DocumentManagerPlugin>('document-manager')!.provides()
    const loaded = () => { setReady(true); setStatus(tr("批注后可保存副本或导出")) }
    if (documents.getActiveDocument()) loaded()
    cleanups.current.push(documents.onDocumentOpened(loaded), documents.onDocumentError((e) => setError(tr("PDF 加载失败：{0}", e.message))))
    cleanups.current.push(value.getPlugin<AnnotationPlugin>('annotation')!.provides().onAnnotationEvent((event) => {
      if (event.type !== 'loaded' && !event.committed) {
        version.current++
        setDirty(true)
        setStatus(tr("批注未保存"))
      }
    }))
    // v2's FreeText blur handler only runs once until another annotation is selected.
    // Keep repeated edits in sync when focus moves to LectureTrace's own toolbar.
    const shadow = viewer.current?.container?.shadowRoot
    // EmbedPDF v2 binds shortcuts to document. Scope them to this reader so multiple
    // windows cannot navigate or undo each other's annotations (or consume note input).
    const commands = value.getPlugin<CommandsPlugin>('commands')!.provides()
    const shortcuts = commands.getAllShortcuts()
    const originals = new Map([...shortcuts.keys()].map((key) => {
      const command = commands.getCommandByShortcut(key)!
      return [command.id, command]
    }))
    for (const command of originals.values()) {
      commands.unregisterCommand(command.id)
      commands.registerCommand({ ...command, shortcuts: [] })
    }
    const keyboard = (event: KeyboardEvent) => {
      const field = event.composedPath()[0]
      if (field instanceof HTMLElement && (field.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(field.tagName))) return
      const key = event.key === ' ' ? 'space' : event.key.toLowerCase()
      const shortcut = [event.ctrlKey && 'ctrl', event.shiftKey && 'shift', event.altKey && 'alt', event.metaKey && 'meta', key].filter(Boolean).sort().join('+')
      const id = shortcuts.get(shortcut)
      if (!id) return
      const command = commands.resolve(id, pdf.id)
      if (command.disabled || !command.visible) return
      event.preventDefault()
      event.stopPropagation()
      commands.execute(id, pdf.id, 'keyboard')
    }
    const container = viewer.current?.container
    container?.addEventListener('keydown', keyboard)
    cleanups.current.push(() => container?.removeEventListener('keydown', keyboard))
    const syncText = (event: Event) => {
      const field = event.composedPath()[0]
      if (!(field instanceof HTMLSpanElement) || !field.isContentEditable) return
      const api = value.getPlugin<AnnotationPlugin>('annotation')!.provides()
      const selected = api.getSelectedAnnotation()?.object
      if (selected?.type === PdfAnnotationSubtype.FREETEXT && selected.contents !== field.innerText) {
        api.updateAnnotation(selected.pageIndex, selected.id, { contents: field.innerText.replace(/\u00a0/g, ' ') })
      }
    }
    shadow?.addEventListener('input', syncText)
    cleanups.current.push(() => shadow?.removeEventListener('input', syncText))
  }

  const save = async (copy: boolean) => {
    if (!registry.current || busyRef.current) return
    busyRef.current = true
    setBusy(true)
    setError(null)
    setStatus(copy ? tr("正在保存副本…") : tr("正在导出…"))
    const snapshot = version.current
    try {
      const annotations = registry.current.getPlugin<AnnotationPlugin>('annotation')!.provides().forDocument(pdf.id)
      annotations.deselectAnnotation()
      // ponytail: v2 FreeText only writes standard-font glyphs. Reject rather than export invisible text;
      // replace this guard when the engine supports embedded Unicode annotation fonts.
      if (annotations.getAnnotations().some(({ commitState, object }) =>
        (commitState === 'new' || commitState === 'dirty') && object.type === PdfAnnotationSubtype.FREETEXT && /[^\x00-\x7F]/.test(object.contents))) {
        throw new Error(tr("文字框暂只支持英文，请将中文改放在「评论」中"))
      }
      await annotations.commit().toPromise()
      const buffer = await registry.current.getPlugin<ExportPlugin>('export')!.provides().forDocument(pdf.id).saveAsCopy().toPromise()
      const name = tr("{0}（批注）.pdf", pdf.name.replace(/\.pdf$/i, '').slice(0, 152))
      let result: PdfDocument | null = null
      if (copy) result = await uploadPdf(videoId, new File([buffer], name, { type: 'application/pdf' }))
      else {
        const downloadUrl = URL.createObjectURL(new Blob([buffer], { type: 'application/pdf' }))
        const link = document.createElement('a')
        link.href = downloadUrl
        link.download = name
        link.click()
        window.setTimeout(() => URL.revokeObjectURL(downloadUrl), 1000)
      }
      savedVersion.current = snapshot
      setDirty(version.current !== snapshot)
      setStatus(copy ? tr("副本已保存到页面列表") : tr("已导出批注 PDF"))
      if (result) onSaved(result)
    } catch (e) {
      setError(tr("{0}失败：{1}。修改仍在，可重试。", copy ? tr("保存") : tr("导出"), errorText(e)))
      setStatus(tr("修改尚未保存"))
    } finally { busyRef.current = false; setBusy(false) }
  }

  return (
    <section className="pdf-doc" aria-label={`PDF：${pdf.name}`}>
      <footer className="pdf-toolbar">
        <span className={dirty ? 'pdf-dirty' : 'muted'} role="status">{systemText(busy ? status : dirty ? tr("批注未保存") : !ready ? status : tr("{0} 页 · 原件保留", pdf.page_count))}</span>
        {dirty && <button className="btn btn-small btn-text" disabled={!ready || busy} onClick={() => void save(true)}>{tr("保存副本")}</button>}
        <details className="pdf-actions">
          <summary>{tr("PDF 操作")}</summary>
          <div className="nd-menu">
            <button disabled={!ready || busy} onClick={() => void save(false)}>{tr("导出 PDF")}</button>
            <button disabled={!ready || busy || (!dirty && version.current === 0)} onClick={() => void save(true)}>{tr("保存副本")}</button>
            <button className="is-danger" disabled={busy} onClick={onDelete}>{tr("移到回收站")}</button>
            <p>{tr("中文批注请用「评论」，文字框暂限英文。保存会新建副本，原件保留。")}</p>
          </div>
        </details>
      </footer>
      {error && <p className="pdf-hint error-text small" role="alert">{systemText(error)} <a href={url} target="_blank" rel="noreferrer">{tr("打开原件")}</a></p>}
      <div className={`pdf-editor${busy ? ' is-busy' : ''}`} {...(busy ? { inert: '' } : {})}>
        <PDFViewer ref={viewer} className="pdf-reader" onReady={onReady} config={{
          wasmUrl: new URL(wasmUrl, window.location.href).href,
          fontFallback: { fonts: { 134: new URL(chineseFont, window.location.href).href }, defaultFont: new URL(chineseFont, window.location.href).href },
          fonts: { ui: null, signature: null },
          stamp: { manifests: [], libraries: [] },
          documentManager: { initialDocuments: [{ url, documentId: pdf.id, name: pdf.name }], maxDocuments: 1 },
          tabBar: 'never', i18n: { defaultLocale: getLocale() },
          theme: { preference: document.documentElement.dataset.theme === 'dark' ? 'dark' : document.documentElement.dataset.theme === 'light' ? 'light' : 'system' },
          annotations: { autoCommit: false, annotationAuthor: 'LectureTrace', autoOpenLinks: false },
          disabledCategories: ['document-open', 'document-close', 'document-export', 'document-print', 'document-protect', 'redaction', 'form', 'insert', 'rotate', 'annotation-stamp'],
        }} />
      </div>
    </section>
  )
}
