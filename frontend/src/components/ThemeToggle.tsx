import { tr, useLocale } from '../i18n.ts'
import { useTheme } from '../theme.ts'

/** 顶栏的浅色 / 深色开关 */
export default function ThemeToggle() {
  useLocale()
  const { theme, toggle } = useTheme()
  const dark = theme === 'dark'
  return (
    <button type="button" className="theme-toggle" role="switch" aria-checked={dark} aria-label={tr("深色模式")} title={dark ? tr("切换到浅色模式") : tr("切换到深色模式")} onClick={toggle}>
      <span className="theme-toggle-track" aria-hidden>
        <svg className="theme-icon is-sun" width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round">
          <circle cx="8" cy="8" r="3" /><path d="M8 1.5v1.6M8 12.9v1.6M1.5 8h1.6M12.9 8h1.6M3.4 3.4l1.1 1.1M11.5 11.5l1.1 1.1M3.4 12.6l1.1-1.1M11.5 4.5l1.1-1.1" />
        </svg>
        <svg className="theme-icon is-moon" width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
          <path d="M13.2 9.6A5.6 5.6 0 016.4 2.8a5.6 5.6 0 106.8 6.8z" />
        </svg>
        <span className="theme-toggle-knob" />
      </span>
    </button>
  )
}
