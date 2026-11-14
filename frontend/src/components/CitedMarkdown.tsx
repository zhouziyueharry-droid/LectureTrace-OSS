import { tr, useLocale } from '../i18n.ts'
import { createContext, memo, useContext, useMemo } from 'react'
import Markdown, { type Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import remarkMath from 'remark-math'
import rehypeKatex from 'rehype-katex'
import { normalizeMathDelimiters } from './markdownMath.ts'
import { formatTime, type Citation } from '../api.ts'
import { parseClock } from '../notes/blocks.ts'

/** 可引用的证据 ID（交接 0001 §6）：页 S012、出现 S012.2、无页时段 N001、讲解片段 T0031 */
export const CITE_ID = 'S\\d{3,}(?:\\.\\d+)?|N\\d{3,}|T\\d{4,}'
const ID = `(?:${CITE_ID})`
const SEP = '\\s*[,，、;；]\\s*'
const ITEM = `(?:T\\d{4,}\\s*[-–—]\\s*T\\d{4,}|${ID})`
// 与后端 citations.py 一致：兼容旧笔记的合并引用及 T 范围，正文不回写。
const CITE_RE = new RegExp(`\\[\\s*(${ITEM}(?:${SEP}${ITEM})*)\\s*\\]`, 'g')
const SPLIT_RE = new RegExp(SEP)
const HREF_PREFIX = '#cite-'
const NAV_RE = new RegExp(`\\[定位:(${ID})\\]`, 'g')
// 笔记里用户自己的时间点 [@1:02:18]：只跳转，不属于课程证据引用
const CLOCK_RE = /\[@((?:\d{1,2}:)?\d{1,2}:\d{2})\]/g
const CLOCK_PREFIX = '#clock-'
const PDF_RE = /\[(PDF:[^\]\s]+)\]/g
export const PdfNavigation = createContext<((citation: Citation) => void) | null>(null)

function expandCitation(item: string): string[] {
  const id = item.trim()
  const range = /^T(\d{4,})\s*[-–—]\s*T(\d{4,})$/.exec(id)
  if (range && Math.max(range[1].length, range[2].length) <= 15) {
    const start = Number(range[1]), end = Number(range[2])
    // Bound untrusted ranges; only server-resolved IDs receive active jump buttons.
    if (end >= start && end - start < 1000) {
      return Array.from({ length: end - start + 1 }, (_, i) => `T${String(start + i).padStart(range[1].length, '0')}`)
    }
  }
  return [id]
}

/** ID 的中文说明，用于悬停提示 */
export function citeKind(id: string): string {
  if (id.startsWith('定位:')) return tr("课程位置 {0}（仅用于定位）", id.slice(3))
  if (id.startsWith('S')) return id.includes('.') ? tr("幻灯片 {0} 的第 {1} 次出现", id.split('.')[0], id.split('.')[1]) : tr("幻灯片 {0}", id)
  if (id.startsWith('N')) return tr("无幻灯片时段 {0}", id)
  return tr("讲解片段 {0}", id)
}

/** 把引用改写成特殊链接，交给下面的 a 渲染器：有时间的渲染为跳转按钮，没有的渲染为不可点的标记 */
function preprocess(content: string): string {
  return content.replace(PDF_RE, (_whole, id: string) => `[PDF](${HREF_PREFIX}${id})`).replace(CLOCK_RE, (_whole, t: string) => `[${t}](${CLOCK_PREFIX}${t})`).replace(NAV_RE, (_whole, id: string) => `[定位:${id}](${HREF_PREFIX}定位:${id})`).replace(CITE_RE, (_whole: string, inner: string) =>
    inner
      .split(SPLIT_RE)
      .flatMap(expandCitation)
      .map((id) => `[${id}](${HREF_PREFIX}${id})`)
      .join(' '),
  )
}

interface Props {
  content: string
  /** 引用 ID → 跳转时间，取回答的 citations（或大纲节点的 start） */
  citations: Citation[]
  onSeek: (t: number) => void
  /**
   * 引用为什么不可跳转：
   * - pending：回答还在生成，引用待校验；
   * - stale：基于旧版本证据，时间戳已停用；
   * 省略时，没有时间的 ID 视为未通过校验。
   */
  inactive?: 'pending' | 'stale'
}

const INACTIVE_TITLE = {
  get pending() { return tr("回答完成后可点击跳转") },
  get stale() { return tr("基于旧版本证据，时间戳已停用") },
  get invalid() { return tr("没有找到这条引用的时间") },
}

function CitedMarkdown({ content, citations, onSeek, inactive }: Props) {
  const locale = useLocale()
  const openPdf = useContext(PdfNavigation)
  const starts = useMemo(() => new Map(citations.map((c) => [c.id, c.start])), [citations])
  const pdfCites = useMemo(() => new Map(citations.filter((c) => c.pdf_id).map((c) => [c.id, c])), [citations])
  const text = useMemo(() => preprocess(normalizeMathDelimiters(content)), [content])

  const components = useMemo<Components>(
    () => ({
      table({ children }) {
        return <div className="md-table-scroll" role="region" aria-label={tr("回答中的表格")} tabIndex={0}><table>{children}</table></div>
      },
      a({ href, children }) {
        if (href?.startsWith(CLOCK_PREFIX)) {
          const label = href.slice(CLOCK_PREFIX.length)
          const sec = parseClock(label)
          if (sec == null) return <span className="cite-badge is-off">{label}</span>
          return (
            <button type="button" className="cite-badge cite-clock" title={tr("跳转到 {0}", label)} onClick={() => onSeek(sec)}>
              <span aria-hidden>▶</span> {label}
            </button>
          )
        }
        if (href?.startsWith(HREF_PREFIX)) {
          let id = href.slice(HREF_PREFIX.length)
          try { id = decodeURIComponent(id) } catch { /* 非法转义保持不可跳转 */ }
          if (id.startsWith('PDF:')) {
            const cite = pdfCites.get(id)
            return cite?.url && openPdf ? <button type="button" className="cite-badge cite-pdf" onClick={() => openPdf(cite)} title={tr("{0} · 第 {1} 页", cite.title, cite.page)}>PDF · {cite.page} {tr("页")}</button> :
              <span className="cite-badge cite-pdf is-off" title={inactive === 'pending' ? tr("回答完成后可打开") : tr("PDF 引用未通过校验或原件已不可用")}>PDF</span>
          }
          const start = inactive === 'stale' ? undefined : starts.get(id)
          const kind = citeKind(id)
          const navigation = id.startsWith('定位:')
          if (start == null || !Number.isFinite(start)) {
            const why = INACTIVE_TITLE[inactive ?? 'invalid']
            return (
              <span className={`cite-badge cite-${navigation ? 'N' : id[0]} is-off`} title={`${kind} · ${why}`}>
                {id}
              </span>
            )
          }
          return (
            <button
              type="button"
              className={`cite-badge cite-${navigation ? 'N' : id[0]}`}
              title={tr("{0} · 跳转到 {1}", kind, formatTime(start))}
              onClick={() => onSeek(start)}
            >
              <span aria-hidden>▶</span> {navigation ? tr("定位 ") : ''}{formatTime(start)}
            </button>
          )
        }
        return (
          <a href={href} target="_blank" rel="noreferrer">
            {children}
          </a>
        )
      },
    }),
    [starts, pdfCites, onSeek, inactive, openPdf, locale],
  )

  return (
    <div className="md">
      <Markdown components={components} remarkPlugins={[remarkGfm, remarkMath]} rehypePlugins={[[rehypeKatex, { trust: false }]]}>{text}</Markdown>
    </div>
  )
}

export default memo(CitedMarkdown)
