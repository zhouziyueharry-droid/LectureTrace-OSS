import { useMemo } from 'react'
import type { Citation } from '../api.ts'
import CitedMarkdown from '../components/CitedMarkdown.tsx'
import { inlineDiff, type Hunk } from './suggestions.ts'

export type Decision = 'accept' | 'reject'

interface Props {
  hunk: Hunk
  decision?: Decision
  /** 原文已被改过，无法自动应用 */
  conflict?: boolean
  citations: Citation[]
  stale: boolean
  onSeek: (t: number) => void
  onDecide: (decision: Decision | undefined) => void
}

const KIND_LABEL = { replace: '改写', insert: '新增', delete: '删除' } as const

function Blocks({ blocks, citations, stale, onSeek }: { blocks: string[]; citations: Citation[]; stale: boolean; onSeek: (t: number) => void }) {
  return <>{blocks.map((b, i) => <CitedMarkdown key={i} content={b} citations={citations} onSeek={onSeek} inactive={stale ? 'stale' : undefined} />)}</>
}

export default function SuggestionCard({ hunk, decision, conflict, citations, stale, onSeek, onDecide }: Props) {
  const inline = useMemo(() => (
    hunk.kind === 'replace' && hunk.baseBlocks.length === 1 && hunk.newBlocks.length === 1
      ? inlineDiff(hunk.baseBlocks[0], hunk.newBlocks[0]) : null
  ), [hunk])
  const shared = { citations, stale, onSeek }

  if (decision === 'reject' && hunk.kind === 'insert') {
    return <div className="sg-settled"><span>已拒绝一处新增</span><button type="button" className="btn btn-small btn-text" onClick={() => onDecide(undefined)}>撤销</button></div>
  }
  if (decision) {
    const accepted = decision === 'accept'
    const blocks = accepted ? hunk.newBlocks : hunk.baseBlocks
    return (
      <div className={`sg-settled-block${accepted ? ' is-accepted' : ''}`}>
        <div className="sg-settled"><span className={`sg-chip${accepted ? ' is-ok' : ''}`}>{accepted ? '已接受' : '已拒绝'}</span>
          <button type="button" className="btn btn-small btn-text" onClick={() => onDecide(undefined)}>撤销</button></div>
        {blocks.length > 0 ? <Blocks blocks={blocks} {...shared} /> : <p className="muted small">（这一处已删除）</p>}
      </div>
    )
  }
  return (
    <section className={`sg-card${conflict ? ' is-conflict' : ''}`} aria-label={`Agent 建议：${KIND_LABEL[hunk.kind]}`}>
      <div className="sg-head">
        <span className="sg-chip">Agent 建议 · {KIND_LABEL[hunk.kind]}</span>
        {conflict && <span className="sg-warn small">这一处你已经改过，无法自动应用</span>}
        <span className="spacer" />
        <button type="button" className="btn btn-small" onClick={() => onDecide('reject')}>{conflict ? '忽略' : '拒绝'}</button>
        {!conflict && <button type="button" className="btn btn-small sg-accept" onClick={() => onDecide('accept')}>接受</button>}
      </div>
      <div className="sg-body">
        {inline ? (
          <p className="sg-inline">{inline.map((seg, i) => seg.type === 'eq' ? <span key={i}>{seg.text}</span>
            : seg.type === 'del' ? <del key={i} className="sg-del">{seg.text}</del> : <ins key={i} className="sg-ins">{seg.text}</ins>)}</p>
        ) : (
          <>
            {hunk.baseBlocks.length > 0 && <div className="sg-old" aria-label="将被替换的原文"><Blocks blocks={hunk.baseBlocks} {...shared} /></div>}
            {hunk.newBlocks.length > 0 && <div className="sg-new" aria-label="建议的新内容"><Blocks blocks={hunk.newBlocks} {...shared} /></div>}
          </>
        )}
      </div>
    </section>
  )
}
