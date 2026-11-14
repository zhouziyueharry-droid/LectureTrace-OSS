import { systemText, tr, useLocale } from '../i18n.ts'
import { useEffect, useState } from 'react'
import {
  cancelCodexLogin, errorText, getCodexSettings, getCodexStatus, loginCodex,
  logoutCodex, saveCodexSettings, type CodexSettings, type CodexStatus,
} from '../api.ts'
import { quotaText } from './QuotaStatus.tsx'

export default function CodexCard() {
  useLocale()
  const [settings, setSettings] = useState<CodexSettings | null>(null)
  const [status, setStatus] = useState<CodexStatus | null>(null)
  const [login, setLogin] = useState<{ login_id: string; auth_url: string } | null>(null)
  const [busy, setBusy] = useState(false)
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let active = true
    getCodexSettings().then((s) => active && setSettings(s), (e) => active && setError(errorText(e)))
    return () => { active = false }
  }, [])

  useEffect(() => {
    if (settings?.provider !== 'codex') return
    let active = true
    const refresh = async () => {
      try {
        const result = await getCodexStatus()
        if (!active) return
        setStatus(result)
        if (result.account?.type === 'chatgpt') setLogin(null)
      } catch (e) {
        if (active) setError(errorText(e))
      }
    }
    void refresh()
    const timer = login ? window.setInterval(() => void refresh(), 3000) : undefined
    return () => { active = false; window.clearInterval(timer) }
  }, [settings?.provider, login])

  const change = (patch: Partial<CodexSettings>) => {
    setSettings((s) => s ? { ...s, ...patch } : s)
    setSaved(false)
  }

  const action = async (run: () => Promise<void>) => {
    setBusy(true)
    setError(null)
    try { await run() } catch (e) { setError(errorText(e)) } finally { setBusy(false) }
  }

  const startLogin = () => void action(async () => {
    const result = await loginCodex()
    const url = new URL(result.auth_url)
    if (url.protocol !== 'https:' || !['auth.openai.com', 'auth.chatgpt.com', 'chatgpt.com'].includes(url.hostname)) {
      await cancelCodexLogin(result.login_id)
      throw new Error(tr("登录服务返回的地址不是官方登录地址"))
    }
    setLogin(result)
  })

  return (
    <section className="panel settings-card" aria-label={tr("问答运行方式")}>
      <div className="panel-head"><h2>{tr("问答运行方式")}</h2></div>
      <div className="panel-body settings-body">
        {error && <p className="error-text small" role="alert">{systemText(error)}</p>}
        {!settings ? <p className="muted">{error ? tr("设置暂不可用") : tr("加载中…")}</p> : (
          <>
            <label className="field">
              <span>{tr("问答服务")}</span>
              <select className="lang-select" value={settings.provider} disabled={busy || !!login} onChange={(e) => change({ provider: e.target.value as CodexSettings['provider'] })}>
                <option value="api">{tr("API 模型")}</option>
                <option value="codex">{tr("Codex（ChatGPT 登录）")}</option>
              </select>
            </label>
            {settings.provider === 'codex' && (
              <>
                <p className="muted small">{tr("使用独立的 Codex 账户登录，消耗 ChatGPT 套餐额度。课程文字和截图会发送给模型；实际用量由任务和读取内容决定。")}</p>
                <div className="small" role="status">
                  <p>{status?.account?.type === 'chatgpt' ? tr("已登录：{0}{1}", status.account.email ?? tr("ChatGPT 账户"), status.account.planType ? `（${status.account.planType}）` : '') : status ? tr("尚未登录 ChatGPT") : tr("正在检查登录状态…")}</p>
                  <p>{quotaText(status?.rate_limits ?? null)}</p>
                  {status?.error && <p className="error-text">{systemText(status.error)}</p>}
                  {status?.workspace_mode === 'native' && <p className="muted">{tr("使用 Codex 原生课程工作区。需要许可或补充信息时，会在对话中询问。")}</p>}
                </div>
                <div className="settings-actions">
                  {login ? (
                    <>
                      <a className="btn btn-primary" href={login.auth_url} target="_blank" rel="noopener noreferrer">{tr("打开官方登录页面")}</a>
                      <button className="btn" disabled={busy} onClick={() => void action(async () => { await cancelCodexLogin(login.login_id); setLogin(null) })}>{tr("取消登录")}</button>
                    </>
                  ) : status?.account?.type === 'chatgpt' ? (
                    <button className="btn" disabled={busy} onClick={() => void action(async () => { await logoutCodex(); setStatus(await getCodexStatus()) })}>{tr("退出登录")}</button>
                  ) : <button className="btn" disabled={busy || status?.available === false} onClick={startLogin}>{tr("准备 ChatGPT 登录")}</button>}
                  <button className="btn" disabled={busy} onClick={() => void action(async () => { setStatus(await getCodexStatus()) })}>{tr("刷新账户和额度")}</button>
                </div>
                <p className="muted small">{tr("模型和推理强度可直接在对话输入框中切换，各对话独立保存。")}</p>
                <details className="settings-details">
                  <summary>{tr("高级设置：课程工作区路径")}</summary>
                  <label className="field"><span>{tr("课程工作区根目录（项目目录之外的绝对路径）")}</span><input value={settings.workspace_root} disabled={busy} onChange={(e) => change({ workspace_root: e.target.value })} spellCheck={false} /></label>
                </details>
              </>
            )}
            <div className="settings-actions">
              <button className="btn btn-primary" disabled={busy || !settings.model.trim() || !settings.workspace_root.trim()} onClick={() => void action(async () => { setSettings(await saveCodexSettings(settings)); setSaved(true) })}>{busy ? tr("处理中…") : tr("保存问答设置")}</button>
              {saved && <span className="ok-text small" role="status">{tr("已保存")}</span>}
            </div>
          </>
        )}
      </div>
    </section>
  )
}
