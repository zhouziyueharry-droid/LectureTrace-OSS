export interface EditorGroup { id: number; tabs: string[]; active: string }
export interface EditorLayout { groups: EditorGroup[]; focused: number; opened: string[]; next: number }

export function initialEditors(tabs: string[] = ['outline'], active = tabs[0] ?? ''): EditorLayout {
  const opened = [...new Set([...tabs, ...(active ? [active] : [])])]
  return { groups: [{ id: 0, tabs: opened, active }], focused: 0, opened, next: 1 }
}

export function openEditor(state: EditorLayout, name: string, beside = false): EditorLayout {
  const existing = state.groups.find(g => g.tabs.includes(name))
  if (existing) return { ...state, focused: existing.id, groups: state.groups.map(g => g.id === existing.id ? { ...g, active: name } : g) }
  const opened = [...state.opened, name]
  if (beside && state.groups.find(g => g.id === state.focused)?.active) {
    // References reuse the adjacent group instead of growing a column per citation.
    const index = state.groups.findIndex(g => g.id === state.focused)
    const neighbor = state.groups[index + 1]
    if (neighbor) return { ...state, opened, focused: neighbor.id, groups: state.groups.map(g => g.id === neighbor.id ? { ...g, tabs: [...g.tabs, name], active: name } : g) }
    return { ...state, opened, focused: state.next, next: state.next + 1, groups: [...state.groups, { id: state.next, tabs: [name], active: name }] }
  }
  return { ...state, opened, groups: state.groups.map(g => g.id === state.focused ? { ...g, tabs: [...g.tabs, name], active: name } : g) }
}

export function closeEditor(state: EditorLayout, name: string): EditorLayout {
  const groups = state.groups.map(g => {
    const tabs = g.tabs.filter(t => t !== name)
    return { ...g, tabs, active: g.active === name ? tabs[Math.max(0, g.tabs.indexOf(name) - 1)] ?? tabs[0] ?? '' : g.active }
  }).filter(g => g.tabs.length || state.groups.length === 1)
  if (!groups.length) return initialEditors([])
  return { ...state, groups, opened: state.opened.filter(t => t !== name), focused: groups.some(g => g.id === state.focused) ? state.focused : groups[groups.length - 1].id }
}

export function splitEditor(state: EditorLayout): EditorLayout {
  const source = state.groups.find(g => g.id === state.focused)!
  const move = source.tabs.length > 1 ? source.active : ''
  const groups = state.groups.map(g => g.id === source.id && move ? { ...g, tabs: g.tabs.filter(t => t !== move), active: g.tabs.find(t => t !== move)! } : g)
  groups.splice(groups.findIndex(g => g.id === source.id) + 1, 0, { id: state.next, tabs: move ? [move] : [], active: move })
  return { ...state, groups, focused: state.next, next: state.next + 1 }
}

export function mergeEditors(state: EditorLayout): EditorLayout {
  return { ...state, groups: [{ id: 0, tabs: state.opened, active: state.groups.find(g => g.id === state.focused)?.active || state.opened[0] || '' }], focused: 0 }
}

export function renameEditor(state: EditorLayout, oldName: string, name: string): EditorLayout {
  const rename = (t: string) => t === oldName ? name : t
  return { ...state, opened: state.opened.map(rename), groups: state.groups.map(g => ({ ...g, tabs: g.tabs.map(rename), active: rename(g.active) })) }
}
