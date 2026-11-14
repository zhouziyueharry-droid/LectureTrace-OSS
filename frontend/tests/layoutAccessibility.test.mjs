// Run with: node frontend/tests/layoutAccessibility.test.mjs
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { pathToFileURL } from 'node:url'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import ts from 'typescript'

const require = createRequire(import.meta.url)
const source = readFileSync(new URL('../src/components/Splitter.tsx', import.meta.url), 'utf8')
const js = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, jsx: ts.JsxEmit.ReactJSX } }).outputText
  .replace(/from (["'])(react(?:\/jsx-runtime)?)\1/g, (_, quote, name) => `from ${quote}${pathToFileURL(require.resolve(name)).href}${quote}`)
  .replace("'../i18n.ts'", JSON.stringify(new URL('../src/i18n.ts', import.meta.url).href))
const { default: Splitter } = await import(`data:text/javascript;base64,${Buffer.from(js).toString('base64')}`)
for (const direction of ['row', 'col']) {
  const html = renderToStaticMarkup(createElement(Splitter, {
    direction, label: '调整区域', controls: 'pane', value: 324, min: 200, max: 640,
    onStart: () => 324, onResize: () => {},
  }))
  for (const attr of ['role="separator"', 'aria-controls="pane"', 'aria-valuenow="324"', 'aria-valuemin="200"', 'aria-valuemax="640"', 'aria-valuetext="324 像素"', `aria-orientation="${direction === 'row' ? 'horizontal' : 'vertical'}"`]) {
    assert.ok(html.includes(attr), `Missing ${attr}`)
  }
}

const css = readFileSync(new URL('../src/styles.css', import.meta.url), 'utf8')
const colors = name => [...css.matchAll(new RegExp(`--${name}: (#\\w{6});`, 'g'))].map(m => m[1])
const luminance = hex => hex.slice(1).match(/../g).map(c => parseInt(c, 16) / 255)
  .map(c => c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4)
  .reduce((sum, c, i) => sum + c * [0.2126, 0.7152, 0.0722][i], 0)
for (const background of ['accent-soft', 'surface-2']) {
  colors('accent-hover').forEach((text, i) => {
    const a = luminance(text), b = luminance(colors(background)[i])
    assert.ok((Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05) >= 4.5, `${text} on ${background} fails AA`)
  })
}
console.log('Splitter semantics and light/dark text contrast passed')
