import { useEffect, useState } from 'react'
import {
  cancelCodexLogin, errorText, getCodexSettings, getCodexStatus, loginCodex,
  logoutCodex, saveCodexSettings, type CodexSettings, type CodexStatus,
} from '../api.ts'
import { quotaText } from './QuotaStatus.tsx'
import StepwiseQaSettings from './StepwiseQaSettings.tsx'

type Props = { onProviderChange?: (provider: CodexSettings['provider']) => void }

export default function CodexCard({ onProviderChange }: Props) {
  const [settings, setSettings] = useState<CodexSettings | null>(null)
  const [status, setStatus] = useState<CodexStatus | null>(null)
  const [login, setLogin] = useState<{ login_id: string; auth_url: string } | null>(null)
  const [busy, setBusy] = useState(false)
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let active = true
    getCodexSettings().then((s) => { if (active) { setSettings(s); onProviderChange?.(s.provider) } }, (e) => active && setError(errorText(e)))
    return () => { active = false }
  }, [onProviderChange])

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
      throw new Error('登录服务返回的地址不是官方登录地址')
    }
    setLogin(result)
  })

  return (
    <section className="panel settings-card" aria-label="问答运行方式">
      <div className="panel-head"><h2>问答运行方式</h2></div>
      <div className="panel-body settings-body">
        {error && <p className="error-text small" role="alert">{error}</p>}
        {!settings ? <p className="muted">{error ? '设置暂不可用' : '加载中…'}</p> : (
          <>
            <label className="field">
              <span>问答服务</span>
              <select className="lang-select" value={settings.provider} disabled={busy || !!login} onChange={(e) => {
                const provider = e.target.value as CodexSettings['provider']
                change({ provider })
                onProviderChange?.(provider)
              }}>
                <option value="api">API 模型</option>
                <option value="codex">Codex（ChatGPT 登录）</option>
                <option value="stepwise_qa">步进式问答 Agent（LangGraph）</option>
              </select>
            </label>
            {settings.provider === 'stepwise_qa' && (
              <>
                <p className="muted small">按教学步骤检索课程证据、组织解释并检查引用与讲解连贯性。点击下方按钮会先保存模型 API 配置，再启用步进式问答 Agent。</p>
                <StepwiseQaSettings onEnable={async () => {
                  if (!settings) throw new Error('问答运行方式尚未加载')
                  setBusy(true)
                  setError(null)
                  try {
                    const updated = await saveCodexSettings({ ...settings, provider: 'stepwise_qa' })
                    setSettings(updated)
                    onProviderChange?.(updated.provider)
                    setSaved(true)
                  } finally { setBusy(false) }
                }} enableBusy={busy} />
              </>
            )}
            {settings.provider === 'codex' && (
              <>
                <p className="muted small">使用独立的 Codex 账户登录，消耗 ChatGPT 套餐额度。课程文字和截图会发送给模型；实际用量由任务和读取内容决定。</p>
                <div className="small" role="status">
                  <p>{status?.account?.type === 'chatgpt' ? `已登录：${status.account.email ?? 'ChatGPT 账户'}${status.account.planType ? `（${status.account.planType}）` : ''}` : status ? '尚未登录 ChatGPT' : '正在检查登录状态…'}</p>
                  <p>{quotaText(status?.rate_limits ?? null)}</p>
                  {status?.error && <p className="error-text">{status.error}</p>}
                  {status?.workspace_mode === 'native' && <p className="muted">使用 Codex 原生课程工作区。需要许可或补充信息时，会在对话中询问。</p>}
                </div>
                <div className="settings-actions">
                  {login ? (
                    <>
                      <a className="btn btn-primary" href={login.auth_url} target="_blank" rel="noopener noreferrer">打开官方登录页面</a>
                      <button className="btn" disabled={busy} onClick={() => void action(async () => { await cancelCodexLogin(login.login_id); setLogin(null) })}>取消登录</button>
                    </>
                  ) : status?.account?.type === 'chatgpt' ? (
                    <button className="btn" disabled={busy} onClick={() => void action(async () => { await logoutCodex(); setStatus(await getCodexStatus()) })}>退出登录</button>
                  ) : <button className="btn" disabled={busy || status?.available === false} onClick={startLogin}>准备 ChatGPT 登录</button>}
                  <button className="btn" disabled={busy} onClick={() => void action(async () => { setStatus(await getCodexStatus()) })}>刷新账户和额度</button>
                </div>
                <p className="muted small">模型和推理强度可直接在对话输入框中切换，各对话独立保存。</p>
                <details className="settings-details">
                  <summary>高级设置：课程工作区路径</summary>
                  <label className="field"><span>课程工作区根目录（项目目录之外的绝对路径）</span><input value={settings.workspace_root} disabled={busy} onChange={(e) => change({ workspace_root: e.target.value })} spellCheck={false} /></label>
                </details>
              </>
            )}
            {settings.provider !== 'stepwise_qa' && <div className="settings-actions">
              <button className="btn btn-primary" disabled={busy || !settings.model.trim() || !settings.workspace_root.trim()} onClick={() => void action(async () => {
                const updated = await saveCodexSettings(settings)
                setSettings(updated)
                onProviderChange?.(updated.provider)
                setSaved(true)
              })}>{busy ? '处理中…' : '保存问答设置'}</button>
              {saved && <span className="ok-text small" role="status">已保存</span>}
            </div>}
          </>
        )}
      </div>
    </section>
  )
}
