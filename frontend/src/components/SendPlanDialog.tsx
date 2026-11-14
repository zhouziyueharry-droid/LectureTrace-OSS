import { systemText, getLocale, tr, useLocale } from '../i18n.ts'
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
const k = (n: number) => getLocale() === 'zh-CN' && n >= 10000 ? tr('{0} 万', (n / 10000).toFixed(1)) : n.toLocaleString(getLocale())

function costText(it: SendPlanItem, fx: number): string {
  if (it.skipped) return '—'
  if (it.local) return tr("无 API 费用")
  if (it.cost_usd_range) {
    const [lo, hi] = it.cost_usd_range
    return lo === hi ? sgd(lo, fx) : `${sgd(lo, fx)}–${sgdNum(hi, fx)}`
  }
  if (it.estimated_cost_usd == null) return tr("无法估算")
  if (it.estimated_cost_usd === 0) return tr("免费")
  return tr("约 {0}", sgd(it.estimated_cost_usd, fx))
}

const TIER_LABEL = { flex: 'Flex', get standard() { return tr("标准") } } as const
const TIER_TITLE = {
  get flex() { return tr("Flex 档：半价，响应较慢；资源不足时会自动按标准价重试，不会中断") },
  get standard() { return tr("标准档") },
} as const

function tokenText(it: SendPlanItem): string | null {
  if (!it.input_tokens_range || !it.output_tokens_range) return null
  const [ilo, ihi] = it.input_tokens_range
  const [olo, ohi] = it.output_tokens_range
  const input = ilo === ihi ? k(ilo) : `${k(ilo)}–${k(ihi)}`
  const img = it.image_tokens ? tr("（含图片 {0}）", k(it.image_tokens)) : ''
  return tr("输入 {0} token{1}；输出约 {2}–{3} token（含推理）", input, img, k(olo), k(ohi))
}

export default function SendPlanDialog({ videoId, open, onClose, onConfirmed }: Props) {
  useLocale()
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
      title={tr("确认处理课程")}
      onClose={() => !busy && onClose()}
      width={600}
      footer={
        <>
          <label className="check">
            <input type="checkbox" checked={remember} onChange={(e) => setRemember(e.target.checked)} />{tr("以后不再询问")}</label>
          <span className="spacer" />
            <button className="btn" disabled={busy} onClick={() => { onClose(); navigate('/settings') }}>{tr("更换翻译服务")}</button>
          <button className="btn" onClick={onClose} disabled={busy}>{tr("暂不处理")}</button>
          <button className="btn btn-primary" onClick={() => void submit()} disabled={busy || !plan || blocked}>
            {busy ? tr("提交中…") : tr("确认处理")}
          </button>
        </>
      }
    >
      <p className="muted small">{tr("音频、语音识别、切分字幕和提取幻灯片已完成。下面列出接下来使用的服务与内容；Ollama 字幕翻译在本机运行。若启用云端大纲，相关字幕和幻灯片仍会发送到对应服务。")}</p>
      {loadError && <p className="error-text">{tr("加载发送清单失败：")}{systemText(loadError)}</p>}
      {!plan && !loadError && <p className="muted">{tr("加载中…")}</p>}
      {plan && (
        <>
          {plan.blocking.length > 0 && (
            <div className="alert alert-error">
              {plan.blocking.map((b) => (
                <div key={systemText(b)}>{systemText(b)}</div>
              ))}
            </div>
          )}
          {plan.warnings.map((w) => (
            <div key={systemText(w)} className="alert alert-warn">
              {systemText(w)}
            </div>
          ))}
          <table className="plan-table">
            <thead>
              <tr>
                <th>{tr("步骤")}</th>
                <th>{tr("处理服务")}</th>
                <th>{tr("内容")}</th>
                <th className="num">{tr("预估费用")}</th>
              </tr>
            </thead>
            <tbody>
              {plan.items.map((it) => (
                <tr key={it.stage} className={it.skipped ? 'plan-row-skipped' : undefined}>
                  <td>
                    <span className="plan-stage">{systemText(it.label || stageLabel(it.stage))}</span>
                    {!it.skipped && it.service_tier && (
                      <span className={`tier-tag tier-${it.service_tier}`} title={TIER_TITLE[it.service_tier]}>
                        {TIER_LABEL[it.service_tier] ?? it.service_tier}
                      </span>
                    )}
                  </td>
                  <td>{systemText(it.provider) || '—'}</td>
                  <td className="small">
                    {systemText(it.desc)}
                    {tokenText(it) && <div className="muted">{tokenText(it)}</div>}
                  </td>
                  <td className="num mono">
                    {costText(it, plan.usd_to_sgd)}
                    {!it.skipped && it.fallback_cost_usd_max != null && (
                      <div className="muted small" title={tr("Flex 资源不足、全部按标准价时的上限")}>{tr("标准价最多")}{sgd(it.fallback_cost_usd_max, plan.usd_to_sgd)}
                      </div>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="plan-total">{tr("预计费用")}{' '}
            <b className="mono">
              {unknownCost ? tr("暂无法完整估算") : plan.cost_sgd_range[1] > 0
                ? plan.cost_sgd_range[0] === plan.cost_sgd_range[1]
                  ? 'S$' + plan.cost_sgd_range[0].toFixed(2)
                  : 'S$' + plan.cost_sgd_range[0].toFixed(2) + ' – ' + plan.cost_sgd_range[1].toFixed(2)
                : tr("无 API 费用")}
            </b>
            {plan.fallback_cost_sgd_max != null &&
              plan.items.some((it) => !it.skipped && it.fallback_cost_usd_max != null) &&
              plan.fallback_cost_sgd_max > plan.cost_sgd_range[1] && (
                <span className="muted small">{tr("Flex 资源不足、全部按标准价时最多约")}<span className="mono">S${plan.fallback_cost_sgd_max.toFixed(2)}</span>
                </span>
              )}
          </p>
          {plan.items.some(it => !it.skipped && it.input_tokens_range) && <p className="muted small plan-note">{tr("输入 token 按实际要发送的内容计算（文字用 OpenAI 分词器，图片按官方 32×32 切块公式）；输出含推理，只能给区间。 价格为 OpenAI 官方价（标准价")}{plan.prices_as_of}
            {plan.flex_prices_as_of ? tr("、Flex 价 {0}", plan.flex_prices_as_of) : ''} {tr("查询，可在设置页修改），汇率 1 USD =")}{' '}
            {plan.usd_to_sgd}{tr("SGD。")}{plan.items.some((it) => !it.skipped && it.service_tier === 'flex') &&
              tr("标为 Flex 的步骤按半价估算；Flex 资源不足时会自动按标准价重试，不会中断，费用可能高于区间。")}
          </p>}
        </>
      )}
      {submitError && <p className="error-text">{systemText(submitError)}</p>}
    </Modal>
  )
}
