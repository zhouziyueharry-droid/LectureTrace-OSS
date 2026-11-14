import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { registerHooks } from 'node:module'
import ts from 'typescript'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

registerHooks({ load(url, context, nextLoad) {
  if (url.endsWith('.tsx')) return { format: 'module', shortCircuit: true, source: ts.transpileModule(readFileSync(new URL(url), 'utf8'), {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext, jsx: ts.JsxEmit.ReactJSX },
  }).outputText }
  return nextLoad(url, context)
} })
const { default: CitedMarkdown } = await import('../src/components/CitedMarkdown.tsx')
const citations = [152, 153, 154].map(n => ({ id: `T0${n}`, start: n * 10 }))
const render = (content, options = {}) => renderToStaticMarkup(createElement(CitedMarkdown, { content, citations, onSeek() {}, ...options }))

test('existing transcript range renders separate timestamp buttons', () => {
  for (const dash of ['-', '–', '—']) {
    const html = render(`[ T0152 ${dash} T0154 ]`)
    assert.equal((html.match(/<button/g) ?? []).length, 3)
    for (const label of ['25:20', '25:30', '25:40']) assert.ok(html.includes(label))
    assert.ok(!html.includes(dash + ' T0154'))
  }
  assert.equal((render('[T0152-T0154, S012]').match(/<button/g) ?? []).length, 3)
})

test('missing or stale evidence never becomes a clickable range member', () => {
  const html = render('[T0152-T0154]', { citations: citations.filter(c => c.id !== 'T0153') })
  assert.equal((html.match(/<button/g) ?? []).length, 2)
  assert.match(html, /is-off[^>]*>T0153<\/span>/)
  assert.equal((render('[T0152-T0154]', { inactive: 'stale' }).match(/<button/g) ?? []).length, 0)
  for (const range of ['T0004-T0001', 'T0001-T99999999999999999999', 'T0001-T1001']) {
    assert.equal((render(`[${range}]`).match(/<button/g) ?? []).length, 0)
  }
})
