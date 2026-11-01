/** 笔记按块编辑：以空行分块，围栏代码和 $$ 公式块内部的空行不分块。 */

export type BlockKind = 'empty' | 'heading' | 'list' | 'quote' | 'code' | 'math' | 'table' | 'rule' | 'para'

const FENCE_OPEN = /^\s{0,3}(`{3,}|~{3,})/

export function parseBlocks(text: string): string[] {
  const lines = text.replace(/\r\n/g, '\n').split('\n')
  const blocks: string[] = []
  let current: string[] = []
  let fence: RegExp | null = null
  let math = false
  const flush = () => {
    if (current.length) blocks.push(current.join('\n'))
    current = []
  }
  for (const line of lines) {
    if (fence) {
      current.push(line)
      if (fence.test(line)) fence = null
      continue
    }
    if (math) {
      current.push(line)
      if (line.trim().endsWith('$$')) math = false
      continue
    }
    if (line.trim() === '') {
      flush()
      continue
    }
    const open = FENCE_OPEN.exec(line)
    if (open) {
      const mark = open[1]
      fence = new RegExp(`^\\s{0,3}${mark[0] === '`' ? '`' : '~'}{${mark.length},}\\s*$`)
    } else {
      const t = line.trim()
      if (t.startsWith('$$') && (t === '$$' || !t.slice(2).includes('$$'))) math = true
    }
    current.push(line)
  }
  flush()
  return blocks.length ? blocks : ['']
}

/** 空块只存在于编辑状态，不写进文件。 */
export function joinBlocks(blocks: string[]): string {
  return blocks.filter((b) => b.trim() !== '').join('\n\n')
}

export function blockKind(block: string): BlockKind {
  const t = block.trimStart()
  if (!t.trim()) return 'empty'
  if (/^(`{3,}|~{3,})/.test(t)) return 'code'
  if (t.startsWith('$$')) return 'math'
  if (/^#{1,6}\s/.test(t)) return 'heading'
  if (/^(?:[-*+]|\d+[.)])\s/.test(t)) return 'list'
  if (t.startsWith('>')) return 'quote'
  if (/^\|/.test(t) && block.includes('\n')) return 'table'
  if (/^(?:-{3,}|\*{3,}|_{3,})\s*$/.test(t)) return 'rule'
  return 'para'
}

/** 光标所在位置是否处在尚未闭合的围栏代码或 $$ 公式里（此时 Enter 只换行）。 */
export function insideOpenFence(block: string, caret: number): boolean {
  const before = block.slice(0, caret)
  const lines = before.split('\n')
  let fence = false
  let math = false
  for (const line of lines) {
    if (fence) { if (/^\s{0,3}(`{3,}|~{3,})\s*$/.test(line)) fence = false; continue }
    if (math) { if (line.trim().endsWith('$$')) math = false; continue }
    if (FENCE_OPEN.test(line)) fence = true
    else {
      const t = line.trim()
      if (t.startsWith('$$') && (t === '$$' || !t.slice(2).includes('$$'))) math = true
    }
  }
  return fence || math
}

/** 列表里按 Enter 的续写：返回要插入的前缀；空列表项返回 null（表示结束列表）。 */
export function listContinuation(block: string, caret: number): { prefix: string } | { end: true } | null {
  const lineStart = block.lastIndexOf('\n', caret - 1) + 1
  const lineEnd = block.indexOf('\n', caret)
  const line = block.slice(lineStart, lineEnd === -1 ? block.length : lineEnd)
  const m = /^(\s*)([-*+]|\d+[.)])(\s+)(\[[ xX]\]\s+)?(.*)$/.exec(line)
  if (!m) return null
  if (m[5].trim() === '') return { end: true }
  let marker = m[2]
  const num = /^(\d+)([.)])$/.exec(marker)
  if (num) marker = `${Number(num[1]) + 1}${num[2]}`
  return { prefix: `${m[1]}${marker}${m[3]}${m[4] ? '[ ] ' : ''}` }
}

/** 解析 [@1:02:18] / [@12:40] 为秒数 */
export function parseClock(text: string): number | null {
  const parts = text.split(':').map(Number)
  if (parts.some((n) => !Number.isFinite(n)) || parts.length < 2 || parts.length > 3) return null
  return parts.reduce((total, n) => total * 60 + n, 0)
}

export function clockToken(sec: number): string {
  const s = Math.max(0, Math.floor(sec))
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const r = s % 60
  const pad = (n: number) => String(n).padStart(2, '0')
  return `[@${h > 0 ? `${h}:${pad(m)}` : String(m)}:${pad(r)}]`
}

/** 点击渲染出来的待办框：切换这个块里第 n 个待办项的勾选 */
export function toggleTask(block: string, n: number): string {
  let seen = -1
  return block.split('\n').map((line) => {
    const m = /^(\s*(?:[-*+]|\d+[.)])\s+\[)([ xX])(\].*)$/.exec(line)
    if (!m || ++seen !== n) return line
    return m[1] + (m[2] === ' ' ? 'x' : ' ') + m[3]
  }).join('\n')
}
