import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, errorText, getNote, saveNote, type NoteDocument } from '../api.ts'
import { joinBlocks, parseBlocks } from '../notes/blocks.ts'
import { mergeBlocks } from '../notes/suggestions.ts'

interface Draft { base: NoteDocument; text: string }

export type SaveState = 'loading' | 'saved' | 'dirty' | 'saving' | 'error' | 'conflict'

const AUTOSAVE_MS = 1000

/**
 * 页面正文：停笔后自动保存，没有保存按钮。
 * 保存遇到别人（Agent 或另一个窗口）改过：不同块的改动自动合并，同一块冲突才请用户选择。
 */
export function useNoteDocument(videoId: string, name: string, refreshKey: number, onChanged: () => void) {
  const key = `lt.noteDraft:${videoId}:${name}`
  const [draft, setDraftState] = useState<Draft | null>(() => {
    try {
      const value = JSON.parse(sessionStorage.getItem(key) ?? 'null')
      return value?.base?.name === name && typeof value.base.revision === 'string' && typeof value.text === 'string' ? value : null
    } catch { return null }
  })
  const [status, setStatusState] = useState<SaveState>(() => (draft ? 'dirty' : 'loading'))
  const [error, setError] = useState<string | null>(null)
  const [latest, setLatest] = useState<NoteDocument | null>(null)
  const [storageError, setStorageError] = useState(false)
  const draftRef = useRef(draft)
  const statusRef = useRef(status)
  const timer = useRef<number | undefined>(undefined)
  const saving = useRef<Promise<void> | null>(null)
  const request = useRef(0)
  const flushRef = useRef<() => Promise<NoteDocument | null>>(async () => null)
  const onChangedRef = useRef(onChanged)
  onChangedRef.current = onChanged

  const setDraft = useCallback((next: Draft | null) => { draftRef.current = next; setDraftState(next) }, [])
  const setStatus = useCallback((next: SaveState) => { statusRef.current = next; setStatusState(next) }, [])

  const dirty = !!draft && draft.text !== draft.base.text

  /** 用别人的最新版本重新对齐：能自动合并就合并，否则进入冲突状态 */
  const reconcile = useCallback((theirs: NoteDocument) => {
    const d = draftRef.current
    if (!d || d.text === d.base.text) { setDraft({ base: theirs, text: theirs.text }); setLatest(theirs); setStatus('saved'); return true }
    const merged = mergeBlocks(parseBlocks(d.base.text), parseBlocks(d.text), parseBlocks(theirs.text))
    if (merged) {
      setDraft({ base: theirs, text: joinBlocks(merged) })
      setLatest(theirs)
      setStatus('dirty')
      window.clearTimeout(timer.current)
      timer.current = window.setTimeout(() => { void flushRef.current() }, 300)
      return true
    }
    setLatest(theirs)
    setStatus('conflict')
    return false
  }, [setDraft, setStatus])

  const writeOnce = useCallback(async () => {
    const d = draftRef.current
    if (!d || d.text === d.base.text || statusRef.current === 'conflict') return
    setStatus('saving')
    setError(null)
    const sent = d.text
    try {
      const note = await saveNote(videoId, name, sent, d.base.revision)
      const current = draftRef.current
      const unchanged = !current || current.text === sent
      setDraft({ base: note, text: unchanged ? note.text : current.text })
      setLatest(note)
      setStatus(unchanged ? 'saved' : 'dirty')
      onChangedRef.current()
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        try {
          const theirs = await getNote(videoId, name)
          if (reconcile(theirs)) return
        } catch (inner) { setError(errorText(inner)); setStatus('error') }
        return
      }
      setError(errorText(e))
      setStatus('error')
    }
  }, [videoId, name, reconcile, setDraft, setStatus])

  /** 立即写入所有改动（切换页面、提问、导出前）；返回写入后的最新版本 */
  const flush = useCallback(async (): Promise<NoteDocument | null> => {
    window.clearTimeout(timer.current)
    for (let round = 0; round < 5; round++) {
      if (saving.current) await saving.current
      const d = draftRef.current
      if (!d) return null
      if (d.text === d.base.text) return d.base
      if (statusRef.current === 'conflict') return null
      saving.current = writeOnce().finally(() => { saving.current = null })
      await saving.current
    }
    return draftRef.current?.base ?? null
  }, [writeOnce])
  flushRef.current = flush

  const refresh = useCallback(async () => {
    if (saving.current) return
    const seq = ++request.current
    try {
      const note = await getNote(videoId, name)
      if (seq !== request.current) return
      setLatest(note)
      const d = draftRef.current
      if (!d) { setDraft({ base: note, text: note.text }); setStatus('saved') }
      else if (note.revision !== d.base.revision) reconcile(note)
      else if (statusRef.current === 'loading') setStatus(d.text === d.base.text ? 'saved' : 'dirty')
      setError(null)
    } catch (e) {
      if (seq === request.current) { setError(errorText(e)); if (!draftRef.current) setStatus('error') }
    }
  }, [videoId, name, reconcile, setDraft, setStatus])

  useEffect(() => {
    void refresh()
    return () => { request.current++ }
  }, [refresh, refreshKey])

  useEffect(() => {
    const onFocus = () => { void refresh() }
    window.addEventListener('focus', onFocus)
    return () => window.removeEventListener('focus', onFocus)
  }, [refresh])

  // 离开页面（切换、关闭标签）时把没写出去的改动写出去
  useEffect(() => () => { window.clearTimeout(timer.current); void flushRef.current() }, [])

  useEffect(() => {
    try {
      if (dirty) sessionStorage.setItem(key, JSON.stringify(draft))
      else sessionStorage.removeItem(key)
      setStorageError(false)
    } catch { setStorageError(true) }
  }, [key, draft, dirty])

  useEffect(() => {
    if (!dirty && status !== 'saving') return
    const warn = (e: BeforeUnloadEvent) => { e.preventDefault(); e.returnValue = '' }
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [dirty, status])

  const edit = useCallback((text: string) => {
    const d = draftRef.current
    if (!d || statusRef.current === 'conflict') return
    setDraft({ ...d, text })
    if (statusRef.current !== 'saving') setStatus(text === d.base.text ? 'saved' : 'dirty')
    window.clearTimeout(timer.current)
    timer.current = window.setTimeout(() => { void flushRef.current() }, AUTOSAVE_MS)
  }, [setDraft, setStatus])

  /** 冲突时的两种选择：用我的版本覆盖，或放弃草稿载入最新版本 */
  const resolveConflict = useCallback((keep: 'mine' | 'theirs') => {
    const d = draftRef.current
    if (!latest || !d) return
    setError(null)
    if (keep === 'theirs') { setDraft({ base: latest, text: latest.text }); setStatus('saved'); return }
    setDraft({ base: latest, text: d.text })
    setStatus('dirty')
    void flushRef.current()
  }, [latest, setDraft, setStatus])

  const retry = useCallback(() => {
    if (statusRef.current === 'error') setStatus(draftRef.current && draftRef.current.text !== draftRef.current.base.text ? 'dirty' : 'saved')
    void flushRef.current()
  }, [setStatus])

  return {
    text: draft?.text ?? '',
    base: draft?.base ?? null,
    loaded: draft !== null,
    status,
    dirty,
    error,
    latest,
    storageError,
    edit,
    flush,
    refresh,
    retry,
    resolveConflict,
  }
}
