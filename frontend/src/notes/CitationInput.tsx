import { useContext, useEffect, useLayoutEffect, useRef, type KeyboardEvent } from 'react'
import type { Citation } from '../api.ts'
import { PdfNavigation } from '../components/CitedMarkdown.tsx'

const PDF = /\[(PDF:[^\]\s]+)\]/g
export type InputSelection = { from: number; to: number }

/** DOM decorations never enter Markdown, clipboard text, or Agent selections. */
export function inputText(node: Node): string {
  if (node.nodeType === Node.TEXT_NODE) return node.textContent ?? ''
  if (node instanceof HTMLElement) {
    if (node.dataset.source) return node.dataset.source
    if (node.dataset.filler) return ''
    if (node.tagName === 'BR') return '\n'
  }
  return Array.from(node.childNodes).map((child, i) =>
    (i > 0 && child instanceof HTMLElement && /^(DIV|P)$/.test(child.tagName) ? '\n' : '') + inputText(child),
  ).join('')
}

export function inputSelection(root: HTMLElement): InputSelection {
  const sel = window.getSelection()
  if (!sel?.rangeCount || !root.contains(sel.anchorNode) || !root.contains(sel.focusNode)) {
    const end = inputText(root).length
    return { from: end, to: end }
  }
  const offset = (node: Node, at: number) => {
    const range = document.createRange()
    range.selectNodeContents(root)
    range.setEnd(node, at)
    return inputText(range.cloneContents()).length
  }
  const range = sel.getRangeAt(0)
  return { from: offset(range.startContainer, range.startOffset), to: offset(range.endContainer, range.endOffset) }
}

export function selectInput(root: HTMLElement, from: number, to = from) {
  const point = (offset: number): [Node, number] => {
    let remaining = Math.max(0, offset)
    for (const child of Array.from(root.childNodes)) {
      const length = inputText(child).length
      if (child.nodeType === Node.TEXT_NODE && remaining <= length) return [child, remaining]
      if (remaining < length) {
        const i = Array.from(root.childNodes).indexOf(child)
        return [root, i + (remaining > 0 ? 1 : 0)]
      }
      remaining -= length
    }
    return [root, root.childNodes.length]
  }
  const range = document.createRange()
  range.setStart(...point(from))
  range.setEnd(...point(to))
  const sel = window.getSelection()
  sel?.removeAllRanges()
  sel?.addRange(range)
}

interface Props {
  id: number
  value: string
  className: string
  placeholder: string
  citations: Citation[]
  onChange: (value: string) => void
  onSelect: (selection: InputSelection) => void
  onKeyDown: (event: KeyboardEvent<HTMLDivElement>) => void
}

export default function CitationInput({ id, value, className, placeholder, citations, onChange, onSelect, onKeyDown }: Props) {
  const root = useRef<HTMLDivElement>(null)
  const composing = useRef(false)
  const openPdf = useContext(PdfNavigation)
  const history = useRef([{ text: value, from: value.length, to: value.length }])
  const cursor = useRef(0)
  const pending = useRef<InputSelection | null>(null)

  const rememberSelection = () => {
    if (!root.current) return
    const selection = inputSelection(root.current)
    Object.assign(history.current[cursor.current], selection)
    onSelect(selection)
  }

  useLayoutEffect(() => {
    const el = root.current!
    if (composing.current) return
    const focused = el === document.activeElement
    const selection = pending.current ?? (focused ? inputSelection(el) : null)
    pending.current = null
    if (history.current[cursor.current].text !== value) {
      history.current.splice(++cursor.current)
      history.current.push({ text: value, ...(selection ?? { from: value.length, to: value.length }) })
      // ponytail: keep 100 block-local snapshots; use editor transactions if cross-block undo is needed.
      if (history.current.length > 100) { history.current.shift(); cursor.current-- }
    }
    const nodes: Node[] = []
    let end = 0
    for (const match of value.matchAll(PDF)) {
      nodes.push(document.createTextNode(value.slice(end, match.index)))
      const cite = citations.find((c) => c.id === match[1])
      const chip = document.createElement('span')
      chip.contentEditable = 'false'
      chip.dataset.source = match[0]
      chip.className = `cite-badge cite-pdf${cite?.url && openPdf ? '' : ' is-off'}`
      const page = cite?.page ?? /^PDF:[^:]+:[^:]+:(\d+)$/.exec(match[1])?.[1]
      chip.textContent = page ? `PDF · ${page} 页` : 'PDF'
      chip.title = cite?.url && openPdf ? `${cite.title} · 第 ${cite.page} 页` : 'PDF 引用未通过校验或原件已不可用'
      chip.setAttribute('role', 'button')
      chip.setAttribute('aria-label', chip.title)
      chip.setAttribute('aria-disabled', String(!cite?.url || !openPdf))
      chip.tabIndex = 0
      nodes.push(chip)
      end = match.index + match[0].length
    }
    nodes.push(document.createTextNode(value.slice(end)))
    if (!value || value.endsWith('\n')) {
      const filler = document.createElement('br')
      filler.dataset.filler = 'true'
      nodes.push(filler)
    }
    el.replaceChildren(...nodes)
    if (selection && focused) selectInput(el, selection.from, selection.to)
  }, [value, citations, openPdf])

  const change = (text: string, selection: InputSelection) => {
    if (text === value) { selectInput(root.current!, selection.from, selection.to); return }
    pending.current = selection
    onChange(text)
  }
  const replaceSelection = (text: string) => {
    const el = root.current!
    const { from, to } = inputSelection(el)
    const source = inputText(el)
    change(source.slice(0, from) + text + source.slice(to), { from: from + text.length, to: from + text.length })
  }
  const undo = (step: number) => {
    const next = cursor.current + step
    if (next < 0 || next >= history.current.length) return
    cursor.current = next
    const entry = history.current[next]
    change(entry.text, entry)
  }
  const open = (target: EventTarget) => {
    const chip = target instanceof HTMLElement ? target.closest<HTMLElement>('[data-source]') : null
    const cite = citations.find((c) => `[${c.id}]` === chip?.dataset.source)
    if (cite?.url) openPdf?.(cite)
    return !!chip
  }

  // Context-menu undo and mobile Enter do not necessarily dispatch keydown.
  useEffect(() => {
    const el = root.current!
    const beforeInput = (event: InputEvent) => {
      if (event.isComposing || composing.current) return
      rememberSelection()
      if (event.inputType === 'historyUndo' || event.inputType === 'historyRedo') {
        event.preventDefault()
        undo(event.inputType === 'historyUndo' ? -1 : 1)
      } else if (event.inputType === 'insertParagraph' || event.inputType === 'insertLineBreak') {
        event.preventDefault()
        replaceSelection('\n')
      } else if (event.inputType.startsWith('format')) event.preventDefault()
    }
    el.addEventListener('beforeinput', beforeInput)
    return () => el.removeEventListener('beforeinput', beforeInput)
  })

  return <div ref={root} data-block={id} className={`${className} blk-capsule-input`} contentEditable suppressContentEditableWarning
    role="textbox" aria-multiline="true" aria-label="编辑段落（Markdown）" data-placeholder={placeholder} spellCheck={false}
    onCompositionStart={() => { composing.current = true }}
    onCompositionEnd={() => {
      composing.current = false
      change(inputText(root.current!), inputSelection(root.current!))
    }}
    onInput={() => { if (!composing.current) change(inputText(root.current!), inputSelection(root.current!)) }}
    onSelect={rememberSelection} onMouseUp={rememberSelection} onKeyUp={rememberSelection}
    onClick={(event) => { open(event.target) }}
    onCopy={(event) => {
      const { from, to } = inputSelection(root.current!)
      if (from === to) return
      event.preventDefault()
      event.clipboardData.setData('text/plain', inputText(root.current!).slice(from, to))
    }}
    onCut={(event) => {
      const { from, to } = inputSelection(root.current!)
      if (from === to) return
      event.preventDefault()
      event.clipboardData.setData('text/plain', inputText(root.current!).slice(from, to))
      replaceSelection('')
    }}
    onPaste={(event) => { event.preventDefault(); replaceSelection(event.clipboardData.getData('text/plain').replace(/\r\n?/g, '\n')) }}
    onDrop={(event) => event.preventDefault()}
    onKeyDown={(event) => {
      if (composing.current || event.nativeEvent.isComposing) return
      if (event.target !== event.currentTarget) {
        if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); open(event.target) }
        const chip = event.target instanceof HTMLElement ? event.target.closest<HTMLElement>('[data-source]') : null
        if (chip && (event.key === 'Backspace' || event.key === 'Delete')) {
          event.preventDefault()
          let from = 0
          for (const child of Array.from(root.current!.childNodes)) {
            if (child === chip) break
            from += inputText(child).length
          }
          root.current!.focus()
          change(value.slice(0, from) + value.slice(from + chip.dataset.source!.length), { from, to: from })
        }
        return
      }
      rememberSelection()
      if ((event.ctrlKey || event.metaKey) && /^(z|y)$/i.test(event.key)) {
        event.preventDefault()
        undo(event.key.toLowerCase() === 'y' || event.shiftKey ? 1 : -1)
        return
      }
      onKeyDown(event)
      if (event.defaultPrevented) return
      if (event.key === 'Enter') { event.preventDefault(); replaceSelection('\n'); return }
      if (event.key === 'Backspace' || event.key === 'Delete') {
        const { from, to } = inputSelection(root.current!)
        if (from !== to) { event.preventDefault(); replaceSelection(''); return }
        for (const match of value.matchAll(PDF)) {
          const end = match.index + match[0].length
          if ((event.key === 'Backspace' && from === end) || (event.key === 'Delete' && from === match.index)) {
            event.preventDefault()
            change(value.slice(0, match.index) + value.slice(end), { from: match.index, to: match.index })
            return
          }
        }
      }
    }} />
}
