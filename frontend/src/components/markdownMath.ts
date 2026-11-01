import { unified } from 'unified'
import remarkParse from 'remark-parse'
import remarkMath from 'remark-math'

const parser = unified().use(remarkParse).use(remarkMath)

/** Convert Agent LaTeX delimiters before Markdown consumes their backslashes. */
export function normalizeMathDelimiters(content: string): string {
  if (!content.includes('\\(') && !content.includes('\\[')) return content
  const protectedRanges: [number, number][] = []
  type Node = { type: string; position?: { start: { offset?: number }; end: { offset?: number } }; children?: Node[] }
  const visit = (node: Node) => {
    if (['code', 'inlineCode', 'math', 'inlineMath'].includes(node.type)) {
      const start = node.position?.start.offset
      const end = node.position?.end.offset
      if (start != null && end != null) protectedRanges.push([start, end])
    } else node.children?.forEach(visit)
  }
  visit(parser.parse(content))
  const convert = (text: string) => text
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
