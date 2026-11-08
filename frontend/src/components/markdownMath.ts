import { unified } from 'unified'
import remarkParse from 'remark-parse'
import remarkMath from 'remark-math'

const parser = unified().use(remarkParse).use(remarkMath)

/** Convert Agent LaTeX delimiters before Markdown consumes their backslashes. */
export function normalizeMathDelimiters(content: string): string {
  const protectedRanges: [number, number][] = []
  type Node = { type: string; position?: { start: { offset?: number }; end: { offset?: number } }; children?: Node[] }
  const visit = (node: Node) => {
    if (['code', 'inlineCode'].includes(node.type)) {
      const start = node.position?.start.offset
      const end = node.position?.end.offset
      if (start != null && end != null) protectedRanges.push([start, end])
    } else node.children?.forEach(visit)
  }
  visit(parser.parse(content))
  const repairEquationLines = (text: string) => {
    let inDisplayMath = false
    return text.split(/(?<=\n)/).map((line) => {
      const body = line.replace(/\r?\n$/, '')
      const ending = line.slice(body.length)
      if (!inDisplayMath && /^\s*=\s*\\(?:left|frac|det|nabla|sum|prod|int|sqrt|partial|cdot|begin|mathrm|text|vec)\b/.test(body)) {
        const formula = body.replace(/\s*\$\$?\s*$/, '').trim()
        return `$$\n${formula}\n$$${ending}`
      }
      const delimiters = body.match(/(?<!\\)\$\$/g)?.length ?? 0
      if (delimiters % 2) inDisplayMath = !inDisplayMath
      return line
    }).join('')
  }
  const convert = (text: string) => repairEquationLines(text)
    .replace(/(?<!\\)\\\(([\s\S]*?)(?<!\\)\\\)/g, (_match, math: string) => `$${math.trim()}$`)
    .replace(/(?<!\\)\\\[([\s\S]*?)(?<!\\)\\\]/g, (_match, math: string) => `\n$$\n${math.trim()}\n$$\n`)
  let result = ''
  let cursor = 0
  for (const [start, end] of protectedRanges) {
    result += convert(content.slice(cursor, start)) + content.slice(start, end)
    cursor = end
  }
  return result + convert(content.slice(cursor))
}
