import { useEffect, useState } from 'react'
import {
  errorText, getStepwiseQaSettings, saveStepwiseQaSettings, testStepwiseQaConnection,
  type StepwiseQaSettings as AgentSettings,
} from '../api.ts'

const BASE_URLS: Record<AgentSettings['provider'], string> = {
  deepseek: 'https://api.deepseek.com',
  openai: 'https://api.openai.com/v1',
  custom: 'https://api.openai.com/v1',
}

interface Props {
  onEnable?: () => Promise<void>
  enableBusy?: boolean
}

export default function StepwiseQaSettings({ onEnable, enableBusy = false }: Props) {
  const [settings, setSettings] = useState<AgentSettings | null>(null)
  const [apiKey, setApiKey] = useState('')
  const [clearKey, setClearKey] = useState(false)
  const [busy, setBusy] = useState(false)
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [testMessage, setTestMessage] = useState<{ ok: boolean; message: string } | null>(null)

  useEffect(() => {
    let active = true
    getStepwiseQaSettings().then(
      (value) => active && setSettings(value),
      (reason) => active && setError(errorText(reason)),
    )
    return () => { active = false }
  }, [])

  const change = (patch: Partial<AgentSettings>) => {
    setSettings((current) => current ? { ...current, ...patch } : current)
    setSaved(false)
    setTestMessage(null)
  }

  const save = async () => {
    if (!settings) return
    const updated = await saveStepwiseQaSettings({
      ...settings,
      api_key: apiKey.trim() || undefined,
      clear_api_key: clearKey,
    })
    setSettings(updated)
    setApiKey('')
    setClearKey(false)
    setSaved(true)
    return updated
  }

  const run = async (action: () => Promise<void>) => {
    setBusy(true)
    setError(null)
    setTestMessage(null)
    try { await action() } catch (reason) { setError(errorText(reason)) } finally { setBusy(false) }
  }

  if (!settings) return <p className="muted small">{error ? `步进式问答 Agent 设置加载失败：${error}` : '正在加载步进式问答 Agent 设置…'}</p>

  return (
    <section className="panel settings-card" aria-label="步进式问答 Agent 模型设置">
      <div className="panel-head"><h2>步进式问答 Agent 模型设置</h2></div>
      <div className="panel-body settings-body">
        <p className="muted small">此处的服务商、API Key 和模型独立于「API 问答模型」设置。Key 保存在本机后端，只返回末 4 位提示。</p>
        <label className="field">
          <span>服务商</span>
          <select value={settings.provider} disabled={busy} onChange={(event) => {
            const provider = event.target.value as AgentSettings['provider']
            change({ provider, base_url: BASE_URLS[provider],
              model: provider === 'deepseek' ? 'deepseek-v4-pro' : provider === 'openai' ? 'gpt-5.6-sol' : settings.model })
          }}>
            <option value="deepseek">DeepSeek</option>
            <option value="openai">OpenAI</option>
            <option value="custom">自定义 OpenAI 兼容接口</option>
          </select>
        </label>
        <label className="field">
          <span>接口地址</span>
          <input value={settings.base_url} disabled={busy} onChange={(event) => change({ base_url: event.target.value })} spellCheck={false} />
        </label>
        <label className="field">
          <span>模型 ID</span>
          <input value={settings.model} disabled={busy} onChange={(event) => change({ model: event.target.value })} spellCheck={false} placeholder="例如 deepseek-v4-pro" />
        </label>
        <label className="field">
          <span>步进式问答 Agent 专属 API Key{settings.has_key && !clearKey ? `（已配置 ${settings.key_hint}）` : ''}</span>
          <input type="password" autoComplete="new-password" value={apiKey} disabled={busy || clearKey}
            onChange={(event) => { setApiKey(event.target.value); setSaved(false); setTestMessage(null) }}
            placeholder={settings.has_key && !clearKey ? '留空表示保留已保存的 Key' : '粘贴 API Key'} />
        </label>
        {settings.has_key && <label className="check small"><input type="checkbox" checked={clearKey} disabled={busy}
          onChange={(event) => { setClearKey(event.target.checked); setApiKey(''); setSaved(false) }} />清除已保存的 Key</label>}
        <div className="model-row">
          <label className="effort-select small"><span className="muted">思考强度</span>
            <select value={settings.effort} disabled={busy} onChange={(event) => change({ effort: event.target.value as AgentSettings['effort'] })}>
              <option value="none">关闭思考</option><option value="low">低</option>
              {settings.provider !== 'deepseek' && <option value="medium">中</option>}
              <option value="high">高</option><option value="max">最高</option>
            </select>
          </label>
          <label className="effort-select small"><span className="muted">回答输出上限（tokens）</span>
            <input type="number" min={1000} max={64000} step={1000} value={settings.max_output_tokens} disabled={busy}
              onChange={(event) => change({ max_output_tokens: Number(event.target.value) })} />
          </label>
        </div>
        <label className="field"><span>回答语言</span>
          <select value={settings.output_language} disabled={busy} onChange={(event) => change({ output_language: event.target.value as AgentSettings['output_language'] })}>
            <option value="zh-CN">简体中文</option><option value="en">English</option>
          </select>
        </label>
        {error && <p className="error-text small" role="alert">{error}</p>}
        {testMessage && <p className={testMessage.ok ? 'ok-text small' : 'error-text small'} role="status">{testMessage.message}</p>}
        <div className="settings-actions">
          <button className="btn btn-primary" disabled={busy || enableBusy || !settings.model.trim() || !settings.base_url.trim() || (!!onEnable && !settings.has_key && !apiKey.trim())}
            onClick={() => void run(async () => {
              await save()
              await onEnable?.()
            })}>
            {busy || enableBusy ? '处理中…' : onEnable ? '保存并启用步进式问答 Agent' : '保存模型 API 设置'}
          </button>
          <button className="btn" disabled={busy || (!settings.has_key && !apiKey.trim()) || clearKey}
            onClick={() => void run(async () => { await save(); setTestMessage(await testStepwiseQaConnection()) })}>
            {busy ? '处理中…' : '保存并测试模型 API'}
          </button>
          {saved && <span className="ok-text small" role="status">已保存</span>}
        </div>
      </div>
    </section>
  )
}
