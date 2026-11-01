import assert from 'node:assert/strict'
import { resolveLayout } from '../src/useLayoutPrefs.ts'

const prefs = { outlineW: 900, chatW: 640, chatOpen: true, chatDock: 'side' as const, videoH: null, videoFloat: false, videoHidden: false, floatBox: null }
// Left video and outline share space: a saved wide outline must not push Agent below.
for (const width of [792, 1024, 1440, 2560]) {
  const fit = resolveLayout(prefs, width)
  assert.equal(fit.chatPlace, 'side')
  assert.ok(width - fit.chatW - 12 >= 480)
}
assert.equal(resolveLayout(prefs, 791).arrangement, 'stack')
// After compact entry closes Agent, it occupies no region; explicit reopening still works.
assert.equal(resolveLayout({ ...prefs, chatOpen: false }, 687).chatPlace, 'hidden')
assert.equal(resolveLayout(prefs, 687).chatPlace, 'below')
assert.equal(resolveLayout({ ...prefs, chatOpen: false }, 1024).chatPlace, 'hidden')
assert.equal(resolveLayout({ ...prefs, chatDock: 'below' }, 1440).chatPlace, 'below')
assert.equal(resolveLayout({ ...prefs, chatDock: 'below', videoFloat: true }, 1024).chatPlace, 'side')
assert.equal(resolveLayout({ ...prefs, chatDock: 'below', videoHidden: true }, 1024).chatPlace, 'side')
assert.equal(resolveLayout({ ...prefs, videoHidden: true }, 687).chatPlace, 'below')
