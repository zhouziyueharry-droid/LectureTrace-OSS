import test from 'node:test'
import assert from 'node:assert/strict'

test('locale survives reload, synchronizes across tabs, and tolerates blocked storage', async () => {
  const storage = new Map([['lt.locale', 'en']])
  const events = new EventTarget()
  globalThis.localStorage = { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value) }
  globalThis.window = { addEventListener: events.addEventListener.bind(events) }
  globalThis.document = { documentElement: { lang: '' } }
  const locale = await import('../src/i18n.ts?storage-test')
  assert.equal(locale.getLocale(), 'en')
  assert.equal(document.documentElement.lang, 'en')
  locale.setLocale('zh-CN')
  assert.equal(storage.get('lt.locale'), 'zh-CN')
  locale.setLocale('en')
  const reloaded = await import('../src/i18n.ts?reload-test')
  assert.equal(reloaded.getLocale(), 'en')

  storage.set('lt.locale', 'zh-CN')
  const change = new Event('storage')
  Object.defineProperty(change, 'key', { value: 'lt.locale' })
  events.dispatchEvent(change)
  assert.equal(locale.getLocale(), 'zh-CN')
  assert.equal(reloaded.getLocale(), 'zh-CN')
  storage.set('lt.locale', 'invalid')
  events.dispatchEvent(change)
  assert.equal(locale.getLocale(), 'zh-CN')
  locale.setLocale('invalid')
  assert.equal(locale.getLocale(), 'zh-CN')

  globalThis.localStorage = { getItem() { throw new Error('blocked') }, setItem() { throw new Error('blocked') } }
  const blocked = await import('../src/i18n.ts?blocked-test')
  assert.equal(blocked.getLocale(), 'zh-CN')
  assert.doesNotThrow(() => blocked.setLocale('en'))
  assert.equal(blocked.getLocale(), 'en')
  assert.equal(document.documentElement.lang, 'en')
  delete globalThis.window
  delete globalThis.document
  delete globalThis.localStorage
})
