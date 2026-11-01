import { useCallback, useEffect, useState } from 'react'

export type Theme = 'light' | 'dark'

const KEY = 'lt.theme'

function stored(): Theme | null {
  try {
    const v = localStorage.getItem(KEY)
    return v === 'light' || v === 'dark' ? v : null
  } catch {
    return null
  }
}

const systemDark = () => typeof window.matchMedia === 'function' && window.matchMedia('(prefers-color-scheme: dark)').matches

/** 启动时应用用户选过的主题；没选过就跟随系统（样式表里的媒体查询负责） */
export function initTheme() {
  const theme = stored()
  if (theme) document.documentElement.dataset.theme = theme
}

/** 浅色 / 深色开关：用户点过之后固定，之前跟随系统 */
export function useTheme() {
  const [theme, setTheme] = useState<Theme>(() => stored() ?? (systemDark() ? 'dark' : 'light'))

  useEffect(() => {
    if (stored() || typeof window.matchMedia !== 'function') return
    const query = window.matchMedia('(prefers-color-scheme: dark)')
    const onChange = () => { if (!stored()) setTheme(query.matches ? 'dark' : 'light') }
    query.addEventListener('change', onChange)
    return () => query.removeEventListener('change', onChange)
  }, [])

  const toggle = useCallback(() => {
    setTheme((current) => {
      const next: Theme = current === 'dark' ? 'light' : 'dark'
      document.documentElement.dataset.theme = next
      try { localStorage.setItem(KEY, next) } catch { /* 存储不可用：本次页面内仍然生效 */ }
      return next
    })
  }, [])

  return { theme, toggle }
}
