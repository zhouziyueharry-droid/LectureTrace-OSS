import assert from 'node:assert/strict'
import { blockKind, clockToken, insideOpenFence, joinBlocks, listContinuation, parseBlocks, parseClock, toggleTask } from '../src/notes/blocks.ts'

// 空行分块；围栏代码与 $$ 公式内部的空行不分块
const text = '# 标题\n\n段落一\n第二行\n\n```py\na = 1\n\nb = 2\n```\n\n$$\nx\n\ny\n$$\n\n- a\n- b\n\n| a | b |\n| - | - |\n| 1 | 2 |'
const blocks = parseBlocks(text)
assert.equal(blocks.length, 6)
assert.equal(blocks[2], '```py\na = 1\n\nb = 2\n```')
assert.equal(blocks[3], '$$\nx\n\ny\n$$')
assert.equal(joinBlocks(blocks), text)
assert.deepEqual(blocks.map(blockKind), ['heading', 'para', 'code', 'math', 'list', 'table'])

// 单行公式不会吞掉后面的内容；未闭合围栏收尾；空文本给一个空块
assert.deepEqual(parseBlocks('$$x$$\n\n后面'), ['$$x$$', '后面'])
assert.deepEqual(parseBlocks('```\n未闭合\n\n仍在代码里'), ['```\n未闭合\n\n仍在代码里'])
assert.deepEqual(parseBlocks(''), [''])
assert.deepEqual(parseBlocks('\n\n  \n'), [''])
assert.deepEqual(parseBlocks('a\r\n\r\nb'), ['a', 'b'])
assert.equal(joinBlocks(['a', '', '  ', 'b']), 'a\n\nb')
assert.equal(blockKind(''), 'empty')
assert.equal(blockKind('> 引用'), 'quote')
assert.equal(blockKind('---'), 'rule')

// Enter 的行为：围栏内只换行，列表续写，空列表项结束列表
assert.equal(insideOpenFence('```\ncode', 8), true)
assert.equal(insideOpenFence('```\ncode\n```\n后', 14), false)
assert.equal(insideOpenFence('$$\nx', 4), true)
assert.deepEqual(listContinuation('- 甲\n- 乙', 7), { prefix: '- ' })
assert.deepEqual(listContinuation('1. 甲', 4), { prefix: '2. ' })
assert.deepEqual(listContinuation('- [ ] 待办', 8), { prefix: '- [ ] ' })
assert.deepEqual(listContinuation('- ', 2), { end: true })
assert.equal(listContinuation('普通段落', 2), null)

// 时间戳往返
assert.equal(parseClock('1:02:18'), 3738)
assert.equal(parseClock('12:40'), 760)
assert.equal(parseClock('abc'), null)
assert.equal(clockToken(3738), '[@1:02:18]')
assert.equal(clockToken(42), '[@0:42]')
assert.equal(parseClock(clockToken(760).slice(2, -1)), 760)

// 待办框切换：只改第 n 个，其他行和普通列表不动
assert.equal(toggleTask('- [ ] 甲\n- [x] 乙\n- 丙', 0), '- [x] 甲\n- [x] 乙\n- 丙')
assert.equal(toggleTask('- [ ] 甲\n- [x] 乙\n- 丙', 1), '- [ ] 甲\n- [ ] 乙\n- 丙')
assert.equal(toggleTask('1. [ ] 甲', 0), '1. [x] 甲')
assert.equal(toggleTask('- 丙', 0), '- 丙')
