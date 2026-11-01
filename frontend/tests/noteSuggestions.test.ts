import assert from 'node:assert/strict'
import { applyHunks, diffBlocks, inlineDiff, locate, mergeBlocks } from '../src/notes/suggestions.ts'

const base = ['# 标题', '甲', '乙', '丙', '丁']

// 相同内容没有差异
assert.deepEqual(diffBlocks(base, [...base]), [])

// 三种差异：替换、插入、删除各一处
const next = ['# 标题', '甲改', '乙', '新增', '丙', '尾']
const hunks = diffBlocks(base, next)
assert.deepEqual(hunks.map((h) => h.kind), ['replace', 'insert', 'replace'])
assert.deepEqual(hunks[0].baseBlocks, ['甲'])
assert.deepEqual(hunks[1].newBlocks, ['新增'])
assert.equal(hunks[1].before, '乙')
assert.deepEqual(hunks[2].baseBlocks, ['丁'])

// 全部接受得到建议的结果；全部拒绝保持原样；只接受一部分按所选应用
const all = new Set(hunks.map((h) => h.id))
assert.deepEqual(applyHunks(base, hunks, all).blocks, next)
assert.deepEqual(applyHunks(base, hunks, new Set()).blocks, base)
assert.deepEqual(applyHunks(base, hunks, new Set(['h1'])).blocks, ['# 标题', '甲', '乙', '新增', '丙', '丁'])

// 纯删除
const del = diffBlocks(base, ['# 标题', '甲', '丁'])
assert.deepEqual(del.map((h) => h.kind), ['delete'])
assert.deepEqual(applyHunks(base, del, new Set(['h0'])).blocks, ['# 标题', '甲', '丁'])

// 用户在建议产出后改过别的块：仍能应用到当前正文
const edited = ['# 标题', '甲', '乙', '丙', '丁', '用户新写的一段']
const applied = applyHunks(edited, hunks, all)
assert.deepEqual(applied.conflicts, [])
assert.deepEqual(applied.blocks, ['# 标题', '甲改', '乙', '新增', '丙', '尾', '用户新写的一段'])

// 用户改过建议要动的那一块：该条记为冲突，不会被改动，其余照常
const mine = ['# 标题', '甲（用户已改）', '乙', '丙', '丁']
const conflicted = applyHunks(mine, hunks, all)
assert.deepEqual(conflicted.conflicts, ['h0'])
assert.deepEqual(conflicted.blocks, ['# 标题', '甲（用户已改）', '乙', '新增', '丙', '尾'])
assert.equal(locate(mine, hunks[0]), null)
assert.deepEqual(locate(mine, hunks[1]), { start: 3, end: 3 })

// 插入锚点块被用户删掉：冲突
assert.equal(locate(['# 标题', '甲', '丙', '丁'], hunks[1]), null)

// 文档开头插入
const head = diffBlocks(['甲'], ['前言', '甲'])
assert.equal(head[0].before, null)
assert.deepEqual(applyHunks(['甲', '乙'], head, new Set(['h0'])).blocks, ['前言', '甲', '乙'])

// 三方合并：不同块的改动自动合并；同一块冲突返回 null
assert.deepEqual(mergeBlocks(['a', 'b', 'c'], ['a', 'B', 'c'], ['a', 'b', 'c', 'd']), ['a', 'B', 'c', 'd'])
assert.deepEqual(mergeBlocks(['a', 'b'], ['a', 'b'], ['x', 'y']), ['x', 'y'])
assert.equal(mergeBlocks(['a', 'b'], ['a', 'B'], ['a', 'b2']), null)

// 词级差异：中文逐字、英文按词；还原后与两侧一致
const segments = inlineDiff('把上游梯度乘上局部梯度，再传给前一层。', '把上游梯度乘上该节点的局部梯度，再传给前一层。')!
assert.equal(segments.filter((s) => s.type !== 'ins').map((s) => s.text).join(''), '把上游梯度乘上局部梯度，再传给前一层。')
assert.equal(segments.filter((s) => s.type !== 'del').map((s) => s.text).join(''), '把上游梯度乘上该节点的局部梯度，再传给前一层。')
assert.ok(segments.some((s) => s.type === 'ins' && s.text === '该节点的'))
assert.equal(inlineDiff('a '.repeat(600), 'b'), null)
