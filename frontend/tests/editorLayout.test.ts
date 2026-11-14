import assert from 'node:assert/strict'
import { initialEditors, openEditor, closeEditor, splitEditor, mergeEditors, renameEditor, editorColumns } from '../src/notes/editorLayout.ts'

let state = initialEditors(['outline', 'notes.md'], 'notes.md')
state = openEditor(state, 'pdf:a', true)
assert.equal(state.groups.length, 2)
state = openEditor(state, 'notes.md')
state = openEditor(state, 'pdf:b', true)
assert.equal(state.groups.length, 2, 'References reuse adjacent group')
assert.deepEqual(state.groups[1].tabs, ['pdf:a', 'pdf:b'])
const order = state.opened
state = openEditor(state, 'pdf:a')
state = splitEditor(state)
assert.deepEqual(state.opened, order, 'Splitting does not reorder mounted document DOM')
assert.equal(state.groups.length, 3)
state = closeEditor(state, 'pdf:a')
assert.equal(state.groups.length, 2)
state = mergeEditors(state)
state = renameEditor(state, 'notes.md', 'renamed.md')
assert.deepEqual(state.groups[0].tabs, ['outline', 'renamed.md', 'pdf:b'])
for (const tab of [...state.opened]) state = closeEditor(state, tab)
assert.equal(state.groups.length, 1)
assert.equal(state.groups[0].active, '')
assert.equal(openEditor(state, 'new.md').groups[0].active, 'new.md')

// Resized groups can retain a total flex weight below 1 after merging/closing.
// CSS Grid leaves free space unused in that case; normalize the surviving tracks.
const resized = splitEditor(initialEditors(['outline', 'note.md'], 'note.md'))
const weights = { 0: 0.2, 1: 0.4 }
assert.equal(editorColumns(mergeEditors(resized).groups, { 0: 0.914762 }), 'minmax(240px, 1fr)')
assert.equal(editorColumns(closeEditor(resized, 'outline').groups, weights), 'minmax(240px, 1fr)')
const tracks = [...editorColumns(resized.groups, weights).matchAll(/([\d.]+)fr/g)].map(m => Number(m[1]))
assert.ok(Math.abs(tracks.reduce((a, b) => a + b, 0) - 2) < 1e-10)
assert.ok(Math.abs(tracks[0] / tracks[1] - 0.5) < 1e-10, 'Keep the chosen width ratio')
