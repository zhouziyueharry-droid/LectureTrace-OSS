import { useEffect, useState } from 'react'
import { errorText, getMcpStatus, type McpStatus } from '../api.ts'
import { systemText, tr, useLocale } from '../i18n.ts'

export function McpStatusDetails({ status }: { status: McpStatus }) {
  const locale = useLocale()
  const states = {
    ready: tr('MCP HTTP 可用'),
    disabled: tr('MCP HTTP 未启用'),
    missing_dependency: tr('未安装 MCP 依赖'),
    starting: tr('MCP HTTP 尚未启动'),
    error: tr('MCP 检查失败'),
  }
  const errors = {
    timeout: tr('检查超时，请重新检查或查看后端日志。'),
    protocol_failed: tr('MCP 协议检查失败，请查看后端日志。'),
    library_unreadable: tr('MCP 可响应，但课程库读取失败。请检查数据目录与后端日志。'),
  }
  return <>
    {status.library_readable && <p className="ok-text">{tr('本机工具与课程库可用')}</p>}
    <p className={status.state === 'ready' ? 'ok-text' : status.state === 'error' ? 'error-text' : ''} role="status">
      <strong>{states[status.state]}</strong>
    </p>
    {status.error_code && <p className="error-text small">{errors[status.error_code]}</p>}
    {status.state === 'disabled' && <p className="muted small">{tr('在项目 .env 中配置 LECTURETRACE_MCP_TOKEN 后重启后端，即可启用 HTTP。此令牌不是模型 API Key。')}</p>}
    {status.state === 'missing_dependency' && <p className="muted small">{tr('请在现有后端环境中安装 MCP extra，并保留已有 ASR/CUDA/OCR 依赖，然后重启后端。')}</p>}
    {status.state === 'starting' && <p className="muted small">{tr('HTTP 服务尚未就绪，请稍后重新检查。')}</p>}
    <p className="small">{tr('访问令牌：')}{status.token_configured ? tr('已配置（不会在页面显示）') : tr('未配置')}</p>
    <p className="small">{tr('课程库读取：')}{status.library_readable === null ? tr('未检查') : status.library_readable ? tr('通过（空课程库也可用）') : tr('失败')}</p>
    {status.protocol_version && <p className="small">{tr('协议版本：')}<code>{status.protocol_version}</code></p>}
    {status.tools.length > 0 && <details className="settings-details">
      <summary>{tr('已发现 {0} 个工具', status.tools.length)}</summary>
      <ul>{status.tools.map(tool => <li key={tool.name}><code>{tool.name}</code></li>)}</ul>
    </details>}
    <p className="muted small">{tr('最近检查：{0} · {1} ms', new Date(status.checked_at).toLocaleString(locale), status.latency_ms)}</p>
  </>
}

export default function McpCard() {
  useLocale()
  const [status, setStatus] = useState<McpStatus | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(true)
  const [revision, setRevision] = useState(0)
  useEffect(() => {
    let active = true
    setBusy(true)
    setError(null)
    setStatus(null)
    getMcpStatus().then(
      result => { if (active) setStatus(result) },
      reason => { if (active) setError(errorText(reason)) },
    ).finally(() => { if (active) setBusy(false) })
    return () => { active = false }
  }, [revision])

  return <section className="panel settings-card" aria-label={tr('本机 MCP')}>
    <div className="panel-head"><h2>{tr('本机 MCP')}</h2></div>
    <div className="panel-body settings-body">
      <p className="muted small">{tr('让支持 MCP 的客户端只读访问本机课程转写、截图与索引。状态检查不会调用模型或重新处理课程。')}</p>
      <p className="small">{tr('HTTP 端点：')}<code>http://127.0.0.1:8000/mcp</code></p>
      {busy && <p className="muted" role="status">{tr('正在检查 MCP…')}</p>}
      {error && <p className="error-text" role="alert">{tr('无法获取 MCP 状态：{0}', systemText(error))}</p>}
      {status && <McpStatusDetails status={status} />}
      <div className="settings-actions"><button className="btn" disabled={busy} onClick={() => { setBusy(true); setRevision(value => value + 1) }}>{tr('重新检查')}</button></div>
      <p className="muted small">{tr('检查本机 MCP 工具与课程库；启用 HTTP 时还检查认证和协议。不代表外部客户端或 tunnel 已连接。')}</p>
      <details className="settings-details">
        <summary>{tr('stdio 的状态怎么看？')}</summary>
        <p className="muted small">{tr('stdio 由每个客户端独立启动，不依赖网页 HTTP 服务或此令牌。请在客户端查看连接状态；本页面无法确认这些独立进程是否已连接。')}</p>
      </details>
    </div>
  </section>
}
