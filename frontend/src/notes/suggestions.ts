/** Agent 建议与冲突合并：按块求差异，再把差异应用到「当前」正文上。 */

export interface Hunk {
  id: string
  kind: 'replace' | 'insert' | 'delete'
  /** 在建议基线块序列里的起点 */
  baseStart: number
  baseBlocks: string[]
  newBlocks: string[]
  /** 紧挨在它前面的、未改动的基线块；插入时用它定位 */
  before: string | null
}

export interface Range { start: number; end: number }

const MAX_CELLS = 2_000_000

export function diffBlocks(base: string[], next: string[]): Hunk[] {
  const n = base.length
  const m = next.length
  if (n === m && base.every((b, i) => b === next[i])) return []
  if ((n + 1) * (m + 1) > MAX_CELLS) {
    return [{ id: 'h0', kind: 'replace', baseStart: 0, baseBlocks: base, newBlocks: next, before: null }]
  }
  const width = m + 1
  const dp = new Int32Array((n + 1) * width)
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i * width + j] = base[i] === next[j] ? dp[(i + 1) * width + j + 1] + 1 : Math.max(dp[(i + 1) * width + j], dp[i * width + j + 1])
    }
  }
  const hunks: Hunk[] = []
  let i = 0
  let j = 0
  let removed: string[] = []
  let added: string[] = []
  let start = 0
  const close = () => {
    if (!removed.length && !added.length) return
    hunks.push({
      id: `h${hunks.length}`,
      kind: removed.length && added.length ? 'replace' : added.length ? 'insert' : 'delete',
      baseStart: start, baseBlocks: removed, newBlocks: added, before: start > 0 ? base[start - 1] : null,
    })
    removed = []
    added = []
  }
  while (i < n || j < m) {
    if (i < n && j < m && base[i] === next[j]) {
      close()
      i++
      j++
      continue
    }
    if (!removed.length && !added.length) start = i
    if (j < m && (i === n || dp[i * width + j + 1] >= dp[(i + 1) * width + j])) added.push(next[j++])
    else removed.push(base[i++])
  }
  close()
  return hunks
}

function sequenceAt(current: string[], seq: string[], at: number): boolean {
  return seq.every((b, k) => current[at + k] === b)
}

/** 找出建议在当前正文里的位置；找不到（用户已经改过那几块）返回 null */
export function locate(current: string[], hunk: Hunk): Range | null {
  if (hunk.kind === 'insert') {
    if (hunk.before === null) return { start: 0, end: 0 }
    const hits = current.flatMap((b, i) => (b === hunk.before ? [i] : []))
    if (!hits.length) return null
    const at = nearest(hits, hunk.baseStart - 1)
    return { start: at + 1, end: at + 1 }
  }
  const len = hunk.baseBlocks.length
  const hits: number[] = []
  for (let p = 0; p + len <= current.length; p++) if (sequenceAt(current, hunk.baseBlocks, p)) hits.push(p)
  if (!hits.length) return null
  const at = nearest(hits, hunk.baseStart)
  return { start: at, end: at + len }
}

function nearest(hits: number[], target: number): number {
  return hits.reduce((best, p) => (Math.abs(p - target) < Math.abs(best - target) ? p : best))
}

export interface Applied { blocks: string[]; conflicts: string[] }

/** 把选中的建议应用到当前块序列；定位不到的记为冲突，不会被改动 */
export function applyHunks(current: string[], hunks: Hunk[], accepted: Set<string>): Applied {
  const located: { hunk: Hunk; range: Range }[] = []
  const conflicts: string[] = []
  for (const hunk of hunks) {
    if (!accepted.has(hunk.id)) continue
    const range = locate(current, hunk)
    if (range) located.push({ hunk, range })
    else conflicts.push(hunk.id)
  }
  // 自后向前应用，前面的下标不受影响；同一位置先改写再插入，插入的块落在改写块之前。
  located.sort((a, b) => b.range.start - a.range.start || (a.hunk.kind === 'insert' ? 1 : 0) - (b.hunk.kind === 'insert' ? 1 : 0))
  const blocks = [...current]
  for (const { hunk, range } of located) blocks.splice(range.start, range.end - range.start, ...hunk.newBlocks)
  return { blocks, conflicts }
}

/** 三方合并：自动合并不同块的改动；同一块冲突返回 null */
export function mergeBlocks(base: string[], mine: string[], theirs: string[]): string[] | null {
  const hunks = diffBlocks(base, mine)
  if (!hunks.length) return theirs
  const { blocks, conflicts } = applyHunks(theirs, hunks, new Set(hunks.map((h) => h.id)))
  return conflicts.length ? null : blocks
}

export interface Segment { type: 'eq' | 'del' | 'ins'; text: string }

const TOKEN = /\s+|[一-鿿]|[A-Za-z0-9_]+|[^\s]/g

/** 单段文字的词级差异；太长时返回 null，由调用方整段对照显示 */
export function inlineDiff(a: string, b: string): Segment[] | null {
  const x = a.match(TOKEN) ?? []
  const y = b.match(TOKEN) ?? []
  if (x.length > 500 || y.length > 500) return null
  const width = y.length + 1
  const dp = new Int32Array((x.length + 1) * width)
  for (let i = x.length - 1; i >= 0; i--) {
    for (let j = y.length - 1; j >= 0; j--) {
      dp[i * width + j] = x[i] === y[j] ? dp[(i + 1) * width + j + 1] + 1 : Math.max(dp[(i + 1) * width + j], dp[i * width + j + 1])
    }
  }
  const out: Segment[] = []
  const push = (type: Segment['type'], text: string) => {
    const last = out[out.length - 1]
    if (last && last.type === type) last.text += text
    else out.push({ type, text })
  }
  let i = 0
  let j = 0
  while (i < x.length || j < y.length) {
    if (i < x.length && j < y.length && x[i] === y[j]) { push('eq', x[i]); i++; j++ }
    else if (j < y.length && (i === x.length || dp[i * width + j + 1] >= dp[(i + 1) * width + j])) push('ins', y[j++])
    else push('del', x[i++])
  }
  return out
}
