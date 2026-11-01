import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { confirmSend, errorText, getSendPlan, stageLabel, type SendPlan, type SendPlanItem } from '../api.ts'
import Modal from './Modal.tsx'

interface Props {
  videoId: string
  open: boolean
  onClose: () => void
  onConfirmed: () => void
}

const sgdNum = (usd: number, fx: number) => (usd * fx).toFixed(2)
const sgd = (usd: number, fx: number) => 'S$' + sgdNum(usd, fx)
const k = (n: number) => (n >= 10000 ? `${(n / 10000).toFixed(1)} 万` : n.toLocaleString('zh-CN'))

function costText(it: SendPlanItem, fx: number): string {
  if (it.skipped) return '—'
  if (it.local) return '无 API 费用'
  if (it.cost_usd_range) {
    const [lo, hi] = it.cost_usd_range
    return lo === hi ? sgd(lo, fx) : `${sgd(lo, fx)}–${sgdNum(hi, fx)}`
  }
  if (it.estimated_cost_usd == null) return '无法估算'
  if (it.estimated_cost_usd === 0) return '免费'
  return `约 ${sgd(it.estimated_cost_usd, fx)}`
}

const TIER_LABEL = { flex: 'Flex', standard: '标准' } as const
const TIER_TITLE = {
  flex: 'Flex 档：半价，响应较慢；资源不足时会自动按标准价重试，不会中断',
  standard: '标准档',
} as const

function tokenText(it: SendPlanItem): string | null {
  if (!it.input_tokens_range || !it.output_tokens_range) return null
  const [ilo, ihi] = it.input_tokens_range
  const [olo, ohi] = it.output_tokens_range
  const input = ilo === ihi ? k(ilo) : `${k(ilo)}–${k(ihi)}`
  const img = it.image_tokens ? `（含图片 ${k(it.image_tokens)}）` : ''
  return `输入 ${input} token${img}；输出约 ${k(olo)}–${k(ohi)} token（含推理）`
}

export default function SendPlanDialog({ videoId, open, onClose, onConfirmed }: Props) {
  const navigate = useNavigate()
  const [plan, setPlan] = useState<SendPlan | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [remember, setRemember] = useState(false)
  const [busy, setBusy] = useState(false)
  const [submitError, setSubmitError] = useState<string | null>(null)

  useEffect(() => {
    if (!open) return
    let cancelled = false
    setPlan(null)
    setLoadError(null)
    setSubmitError(null)
    getSendPlan(videoId).then(
      (p) => !cancelled && setPlan(p),
      (e) => !cancelled && setLoadError(errorText(e)),
    )
    return () => {
      cancelled = true
    }
  }, [open, videoId])

  const submit = async () => {
    setBusy(true)
    setSubmitError(null)
    try {
      await confirmSend(videoId, remember)
      onConfirmed()
    } catch (e) {
      setSubmitError(errorText(e))
    } finally {
      setBusy(false)
    }
  }

  const blocked = (plan?.blocking.length ?? 0) > 0
  const unknownCost = plan?.cost_complete === false || plan?.items.some(it => !it.skipped && it.estimated_cost_usd == null && !it.cost_usd_range)

  return (
    <Modal
      open={open}
      title="确认处理课程"
      onClose={() => !busy && onClose()}
      width={600}
      footer={
        <>
          <label className="check">
            <input type="checkbox" checked={remember} onChange={(e) => setRemember(e.target.checked)} />
            以后不再询问
          </label>
          <span className="spacer" />
            <button className="btn" disabled={busy} onClick={() => { onClose(); navigate('/settings') }}>
              更换翻译服务
            </button>
          <button className="btn" onClick={onClose} disabled={busy}>
            暂不处理
          </button>
          <button className="btn btn-primary" onClick={() => void submit()} disabled={busy || !plan || blocked}>
            {busy ? '提交中…' : '确认处理'}
          </button>
        </>
      }
    >
      <p className="muted small">
        音频、语音识别、切分字幕和提取幻灯片已完成。下面列出接下来使用的服务与内容；Ollama 字幕翻译在本机运行。若启用云端大纲，相关字幕和幻灯片仍会发送到对应服务。
      </p>
      {loadError && <p className="error-text">加载发送清单失败：{loadError}</p>}
      {!plan && !loadError && <p className="muted">加载中…</p>}
      {plan && (
        <>
          {plan.blocking.length > 0 && (
            <div className="alert alert-error">
              {plan.blocking.map((b) => (
                <div key={b}>{b}</div>
              ))}
            </div>
          )}
          {plan.warnings.map((w) => (
            <div key={w} className="alert alert-warn">
              {w}
            </div>
          ))}
          <table className="plan-table">
            <thead>
              <tr>
                <th>步骤</th>
                <th>处理服务</th>
                <th>内容</th>
                <th className="num">预估费用</th>
              </tr>
            </thead>
            <tbody>
              {plan.items.map((it) => (
                <tr key={it.stage} className={it.skipped ? 'plan-row-skipped' : undefined}>
                  <td>
                    <span className="plan-stage">{it.label || stageLabel(it.stage)}</span>
                    {!it.skipped && it.service_tier && (
                      <span className={`tier-tag tier-${it.service_tier}`} title={TIER_TITLE[it.service_tier]}>
                        {TIER_LABEL[it.service_tier] ?? it.service_tier}
                      </span>
                    )}
                  </td>
                  <td>{it.provider || '—'}</td>
                  <td className="small">
                    {it.desc}
                    {tokenText(it) && <div className="muted">{tokenText(it)}</div>}
                  </td>
                  <td className="num mono">
                    {costText(it, plan.usd_to_sgd)}
                    {!it.skipped && it.fallback_cost_usd_max != null && (
                      <div className="muted small" title="Flex 资源不足、全部按标准价时的上限">
                        标准价最多 {sgd(it.fallback_cost_usd_max, plan.usd_to_sgd)}
                      </div>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="plan-total">
            预计费用{' '}
            <b className="mono">
              {unknownCost ? '暂无法完整估算' : plan.cost_sgd_range[1] > 0
                ? plan.cost_sgd_range[0] === plan.cost_sgd_range[1]
                  ? 'S$' + plan.cost_sgd_range[0].toFixed(2)
                  : 'S$' + plan.cost_sgd_range[0].toFixed(2) + ' – ' + plan.cost_sgd_range[1].toFixed(2)
                : '无 API 费用'}
            </b>
            {plan.fallback_cost_sgd_max != null &&
              plan.items.some((it) => !it.skipped && it.fallback_cost_usd_max != null) &&
              plan.fallback_cost_sgd_max > plan.cost_sgd_range[1] && (
                <span className="muted small">
                  Flex 资源不足、全部按标准价时最多约 <span className="mono">S${plan.fallback_cost_sgd_max.toFixed(2)}</span>
                </span>
              )}
          </p>
          {plan.items.some(it => !it.skipped && it.input_tokens_range) && <p className="muted small plan-note">
            输入 token 按实际要发送的内容计算（文字用 OpenAI 分词器，图片按官方 32×32 切块公式）；输出含推理，只能给区间。
            价格为 OpenAI 官方价（标准价 {plan.prices_as_of}
            {plan.flex_prices_as_of ? `、Flex 价 ${plan.flex_prices_as_of}` : ''} 查询，可在设置页修改），汇率 1 USD ={' '}
            {plan.usd_to_sgd} SGD。
            {plan.items.some((it) => !it.skipped && it.service_tier === 'flex') &&
              '标为 Flex 的步骤按半价估算；Flex 资源不足时会自动按标准价重试，不会中断，费用可能高于区间。'}
          </p>}
        </>
      )}
      {submitError && <p className="error-text">{submitError}</p>}
    </Modal>
  )
}
