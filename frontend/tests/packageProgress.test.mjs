// Run with: node frontend/tests/packageProgress.test.mjs
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { prepareCoursePackage, askStream } from '../src/api.ts'
const originalFetch = globalThis.fetch
const mockStream = (events) => {
  const bytes = new TextEncoder().encode(': ping\r\n\r\n' + events.map(e => `data: ${JSON.stringify(e)}\r\n\r\n`).join(''))
  globalThis.fetch = async () => new Response(new ReadableStream({ start(controller) {
    // Split both delimiters and Chinese UTF-8 characters across network chunks.
    for (let offset = 0; offset < bytes.length; offset += 7) controller.enqueue(bytes.slice(offset, offset + 7))
    controller.close()
  } }), { headers: { 'Content-Type': 'text/event-stream' } })
}
try {
  const progress = { type: 'progress', phase: 'writing', written_bytes: 2, total_bytes: 4, lecture_index: 1, lecture_count: 2, lecture_title: '第一讲' }
  const result = { token: 'test', download_url: '/download', filename: 'course.ltcourse' }
  mockStream([progress, { type: 'done', result }])
  const received = []
  assert.deepEqual(await prepareCoursePackage('course', value => received.push(value)), result)
  assert.deepEqual(received, [progress])
  mockStream([{ type: 'error', detail: '磁盘空间不足', status: 400 }])
  await assert.rejects(prepareCoursePackage('course', () => {}), /磁盘空间不足/)
  mockStream([progress])
  await assert.rejects(prepareCoursePackage('course', () => {}), /未确认打包完成/)
  mockStream([{ type: 'token', text: '讲义回答' }, { type: 'done' }])
  const chat = []
  await askStream('video', 'question', value => chat.push(value))
  assert.equal(chat[0].text, '讲义回答')
  assert.equal(chat[1].type, 'done')
} finally {
  globalThis.fetch = originalFetch
}
console.log('Package progress, failures, UTF-8 chunking and shared chat stream passed')
