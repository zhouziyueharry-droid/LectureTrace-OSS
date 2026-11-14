import { tr } from '../i18n.ts'
import type { Outline, OutlineNode } from '../api.ts'
import { clockToken } from './blocks.ts'

const at = (node: OutlineNode) => (Number.isFinite(node.start) ? ` ${clockToken(node.start)}` : '')

function children(nodes: OutlineNode[] | undefined, depth: number): string[] {
  return (nodes ?? []).flatMap((n) => {
    const pad = '  '.repeat(depth)
    return [`${pad}- **${n.title}**${at(n)}${n.summary ? ` — ${n.summary}` : ''}`, ...children(n.children, depth + 1)]
  })
}

/** 把生成的大纲复制成可自由编辑的 Markdown；时间点用 [@时:分:秒]，点击仍可跳转视频 */
export function outlineToMarkdown(outline: Outline, title = tr("大纲")): string {
  const lines = [`# ${title}`]
  for (const chapter of outline.chapters) {
    lines.push('', `## ${chapter.title}${at(chapter)}`)
    if (chapter.summary) lines.push('', chapter.summary)
    const kids = children(chapter.children, 0)
    if (kids.length) lines.push('', ...kids)
  }
  if (outline.admin?.items.length) {
    lines.push('', `## ${outline.admin.title}`, '', ...outline.admin.items.map((n) => `- ${n.title}${at(n)}${n.summary ? ` — ${n.summary}` : ''}`))
  }
  return lines.join('\n')
}
