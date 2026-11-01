import assert from 'node:assert/strict'
import { quoteQuestion } from '../src/outlineQuote.ts'

assert.equal(quoteQuestion('为什么？', null), '为什么？')
assert.equal(
  quoteQuestion('为什么？', { node_id: 'S012', title: '理论与实践', excerpt: '课程涉及数学与实践。' }),
  '为什么？\n\n引用的大纲原文（理论与实践）：\n课程涉及数学与实践。',
)
