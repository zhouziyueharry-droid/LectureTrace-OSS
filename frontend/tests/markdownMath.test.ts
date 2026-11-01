import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import remarkMath from 'remark-math'
import rehypeKatex from 'rehype-katex'
import { normalizeMathDelimiters } from '../src/components/markdownMath.ts'

// The stylesheet imported by main.tsx must match rehype-katex's renderer.
const require = createRequire(import.meta.url)
const rendererRequire = createRequire(require.resolve('rehype-katex'))
assert.equal(require('katex').version, rendererRequire('katex').version, 'KaTeX CSS and renderer versions must match')

const render = (source: string) => renderToStaticMarkup(createElement(Markdown, {
  remarkPlugins: [remarkGfm, remarkMath],
  rehypePlugins: [[rehypeKatex, { trust: false }]],
  children: normalizeMathDelimiters(source),
}))
const answer = String.raw`内积 \(a^\top b\)，范数 \(\|a\|_2=\sqrt{a^\top a}\)，相似度 \(\frac{a^\top b}{\|a\|_2\|b\|_2}\)。[17:22](#cite-T0031)`
const html = render(answer)
assert.equal((html.match(/class="katex"/g) ?? []).length, 3)
assert.ok(html.includes('href="#cite-T0031"'))
assert.ok(!html.includes('katex-error'))
assert.ok(render(String.raw`\[A_{m\times n}B_{n\times p}\]`).includes('katex-display'))
assert.equal((render('$x^2$\n\n$$\nx+y\n$$').match(/class="katex"/g) ?? []).length, 2)
for (const code of ['`\\(x\\)`', '``\\(x\\) ` code``', '```tex\n\\(x\\)\n```', '~~~tex\n\\[x\\]\n~~~', '    \\(x\\)']) {
  assert.equal(normalizeMathDelimiters(code), code)
  assert.ok(!render(code).includes('class="katex"'))
}
assert.equal(normalizeMathDelimiters(String.raw`未完成 \(x^2`), String.raw`未完成 \(x^2`)
assert.doesNotThrow(() => render(String.raw`\(\frac{a}{\)`))
assert.ok(!render(String.raw`\(\href{javascript:alert(1)}{x}\)`).includes('href="javascript:'))
console.log('Math rendering: formulas, citations, code, partial output and unsafe commands passed')
