import { useSyncExternalStore } from 'react'
import { messages } from './locales/en.ts'
import { systemMessages } from './locales/system-en.ts'

export type Locale = 'zh-CN' | 'en'
const STORAGE_KEY = 'lt.locale'
const listeners = new Set<() => void>()

function storedLocale(): Locale {
  try { return localStorage.getItem(STORAGE_KEY) === 'en' ? 'en' : 'zh-CN' }
  catch { return 'zh-CN' }
}

let locale: Locale = storedLocale()
export const getLocale = (): Locale => locale

function applyLocale(next: Locale) {
  locale = next
  if (typeof document !== 'undefined') document.documentElement.lang = next
  listeners.forEach(listener => listener())
}

export function setLocale(next: Locale) {
  if (next !== 'zh-CN' && next !== 'en') return
  try { localStorage.setItem(STORAGE_KEY, next) } catch { /* Keep the session usable without storage. */ }
  applyLocale(next)
}

if (typeof window !== 'undefined') {
  applyLocale(locale)
  window.addEventListener('storage', event => {
    if (event.key === STORAGE_KEY || event.key === null) applyLocale(storedLocale())
  })
}

function subscribe(listener: () => void) {
  listeners.add(listener)
  return () => { listeners.delete(listener) }
}

export function useLocale() {
  return useSyncExternalStore(subscribe, getLocale, getLocale)
}

/** Translate application copy only; never pass course or user-authored content. */
export function tr(source: string, ...values: (string | number | null | undefined)[]): string {
  const text = locale === 'en' ? messages[source] ?? source : source
  return text.replace(/\{(\d+)\}/g, (token, index: string) => Number(index) < values.length ? String(values[Number(index)] ?? '') : token)
}

const copy = { ...messages, ...systemMessages }
const reverse = Object.fromEntries(Object.entries(copy).map(([zh, en]) => [en, zh]))
const escapeRegex = (text: string) => text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
function patterns(dictionary: Record<string, string>) {
  return Object.entries(dictionary).filter(([key]) => /\{\d+\}/.test(key))
    .sort(([a], [b]) => b.replace(/\{\d+\}/g, '').length - a.replace(/\{\d+\}/g, '').length)
    .map(([key, target]) => {
      const slots = [...key.matchAll(/\{(\d+)\}/g)].map(match => Number(match[1]))
      return { regex: new RegExp('^' + key.split(/\{\d+\}/).map(escapeRegex).join('([\\s\\S]*?)') + '$'), slots, target }
    })
}
const translatedPatterns = { en: patterns(copy), 'zh-CN': patterns(reverse) }

/** Known system messages only. Interpolated names, paths and diagnostics stay intact. */
export function systemText(source: string | null | undefined): string {
  if (!source) return ''
  const dictionary = locale === 'en' ? copy : reverse
  if (dictionary[source]) return dictionary[source]
  if (source.length <= 4000) {
    // These pipeline descriptions are composed from independent clauses, not user text.
    if (locale === 'en' && /^(字幕原文 |幻灯片截图 |短索引：|证据：|切分出 |大纲共 )/.test(source) && source.includes('；')) {
      return source.split(/(?=；)/).map(part => systemText(part)).join('')
    }
    for (const { regex, slots, target } of translatedPatterns[locale]) {
      const match = regex.exec(source)
      if (!match) continue
      const values = new Map(slots.map((slot, index) => [slot, match[index + 1]]))
      return target.replace(/\{(\d+)\}/g, (token, slot: string) => values.get(Number(slot)) ?? token)
    }
  }
  return source
}
