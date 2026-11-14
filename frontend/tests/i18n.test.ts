import test from 'node:test'
import assert from 'node:assert/strict'
import { getLocale, setLocale, systemText, tr } from '../src/i18n.ts'
import { messages } from '../src/locales/en.ts'
import { systemMessages } from '../src/locales/system-en.ts'
import { stageLabel, errorText } from '../src/api.ts'

test('language switching, interpolation and fallback', () => {
  setLocale('zh-CN')
  assert.equal(tr('界面语言'), '界面语言')
  setLocale('en')
  assert.equal(getLocale(), 'en')
  assert.equal(tr('界面语言'), 'Interface language')
  assert.equal(tr('unlisted {0}', '$&'), 'unlisted $&')
  assert.equal(tr('unlisted {0}'), 'unlisted {0}')
  setLocale('zh-CN')
})

test('system messages translate without changing inserted names or diagnostics', () => {
  setLocale('en')
  assert.equal(systemText('已翻译 12/30 条'), 'Translated 12/30 cues')
  assert.equal(systemText('本机 Ollama 可连接，已安装 hy-mt2（未运行翻译）'), 'Local Ollama is reachable and hy-mt2 is installed (translation was not run)')
  assert.equal(systemText('已移到回收站：「课程」'), 'Moved to trash: “课程”')
  assert.equal(systemText('字幕原文 12 条（待翻译 5 条），约 15,000 字符；本期已用 10 / 100 字符'), '12 original subtitle cues (5 awaiting translation), about 15,000 characters; 10 / 100 characters used this period')
  assert.equal(systemText('；unknown diagnostic'), '；unknown diagnostic')
  assert.equal(systemText('大纲共 3 章，课程事务 2 条，1 条警告'), 'Outline: 3 chapters, 2 course administration items, 1 warnings')
  assert.equal(systemText('ProviderError: something new'), 'ProviderError: something new')
  assert.equal(errorText(new Error('视频不存在')), 'Video not found')
  assert.equal(stageLabel('asr'), 'Speech recognition')
  setLocale('zh-CN')
  assert.equal(systemText('Translated 12/30 cues'), '已翻译 12/30 条')
  assert.equal(systemText('Moved to trash: “课程”'), '已移到回收站：「课程」')
  assert.equal(stageLabel('asr'), '语音识别')
})

test('all translations preserve interpolation slots and contain English copy', () => {
  const slots = (s: string) => [...s.matchAll(/\{\d+\}/g)].map(m => m[0]).sort()
  for (const [source, translated] of Object.entries({ ...messages, ...systemMessages })) {
    assert.ok(translated.trim(), `Empty translation: ${source}`)
    assert.deepEqual(slots(translated), slots(source), `Changed interpolation: ${source}`)
    assert.doesNotMatch(translated, /[\u3400-\u9fff]/u, `Untranslated copy: ${source}`)
  }
})
