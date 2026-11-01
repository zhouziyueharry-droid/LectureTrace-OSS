import assert from 'node:assert/strict'
import { initialEditors, openEditor, closeEditor, splitEditor, mergeEditors, renameEditor } from '../src/notes/editorLayout.ts'

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
