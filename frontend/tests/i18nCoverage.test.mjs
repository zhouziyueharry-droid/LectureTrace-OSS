import test from 'node:test'
import assert from 'node:assert/strict'
import { readdirSync, readFileSync } from 'node:fs'
import ts from 'typescript'
import { messages } from '../src/locales/en.ts'

test('every literal translation key exists; JSX copy has no unlocalized Chinese', () => {
  const root = new URL('../src/', import.meta.url)
  const failures = []
  for (const file of readdirSync(root, { recursive: true }).filter(name => /\.tsx?$/.test(name) && !name.startsWith('locales'))) {
    const source = readFileSync(new URL(file.replaceAll('\\', '/'), root), 'utf8')
    const tree = ts.createSourceFile(file, source, ts.ScriptTarget.Latest, true)
    const report = (node, text) => failures.push(`${file}:${tree.getLineAndCharacterOfPosition(node.getStart(tree)).line + 1}: ${text}`)
    function visit(node) {
      if (ts.isCallExpression(node) && node.expression.getText(tree) === 'tr' && ts.isStringLiteral(node.arguments[0])) {
        if (!messages[node.arguments[0].text]) report(node, `Missing key ${node.arguments[0].text}`)
      }
      if (ts.isJsxText(node) && /[\u3400-\u9fff]/u.test(node.text)) {
        // Native-language names keep the selector recognizable in either language.
        if (node.text.trim() !== '简体中文') report(node, `Untranslated JSX ${node.text.trim()}`)
      }
      if (ts.isJsxAttribute(node) && node.initializer && ts.isStringLiteral(node.initializer) && /[\u3400-\u9fff]/u.test(node.initializer.text)) report(node, 'Untranslated attribute')
      ts.forEachChild(node, visit)
    }
    visit(tree)
  }
  assert.deepEqual(failures, [])
})
