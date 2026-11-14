import { systemText, tr, useLocale } from '../i18n.ts'
import { useState } from 'react'
import {
  errorText,
  listModels,
  saveLlmSettings,
  type LlmPurpose,
  type LlmSettings,
  type LlmUpdate,
  type ModelPrice,
  type ServiceTier,
} from '../api.ts'

/** 模型列表里排除非对话模型 */
const NON_CHAT = /(embedding|tts|whisper|dall-e|image|audio|realtime|transcribe|moderation|search|computer-use)/i

const EFFORT_LABELS: Record<string, string> = {
  get none() { return tr("none（不思考）") },
  low: 'low',
  get medium() { return tr("medium（默认）") },
  high: 'high',
  xhigh: 'xhigh',
  get max() { return tr("max（最贵）") },
}

const PRICE_FIELDS = [
  { key: 'input', get label() { return tr("输入") } },
  { key: 'cached_input', get label() { return tr("缓存输入") } },
  { key: 'output', get label() { return tr("输出") } },
] as const

const FLEX_PRICE_FIELDS = [
  { key: 'flex_input', get label() { return tr("输入") } },
  { key: 'flex_cached_input', get label() { return tr("缓存输入") } },
  { key: 'flex_output', get label() { return tr("输出") } },
] as const

const TIER_LABELS: Record<string, string> = {
  get flex() { return tr("省钱（Flex，半价，较慢）") },
  get standard() { return tr("标准") },
}

function priceLine(p: ModelPrice | undefined, tier: ServiceTier | undefined): string {
  if (!p) return tr("价格表里没有这个模型，费用无法估算，请在下方填写价格")
  const note = p.note ? ' · ' + systemText(p.note) : ''
  if (tier === 'flex') {
    if (p.flex_input == null || p.flex_output == null) {
      return tr("价格表里没有这个模型的 Flex 价，按标准价估算：输入 ${0} / 输出 ${1}（每百万 token）{2}", p.input, p.output, note)
    }
    const cached = p.flex_cached_input != null ? tr(" / 缓存 ${0}", p.flex_cached_input) : ''
    return tr("Flex 价：输入 ${0}{1} / 输出 ${2}（每百万 token；标准价 ${3} / ${4}）{5}", p.flex_input, cached, p.flex_output, p.input, p.output, note)
  }
  const cached = p.cached_input != null ? tr(" / 缓存 ${0}", p.cached_input) : ''
  return tr("输入 ${0}{1} / 输出 ${2}（每百万 token）{3}", p.input, cached, p.output, note)
}

interface Props {
  section: 'outline' | 'translation' | 'qa' | 'advanced'
  llm: LlmSettings
  hasKey: boolean
  hasDeepseekKey?: boolean
  onSaved: (llm: LlmSettings) => void
  onOpenConnection: () => void
}

/** 设置页：各用途用哪个模型、价格表、汇率、是否自动生成大纲 */
export default function LlmCard({ section, llm, hasKey, hasDeepseekKey, onSaved, onOpenConnection }: Props) {
  useLocale()
  const [models, setModels] = useState<Record<LlmPurpose, string>>(llm.models)
  const [efforts, setEfforts] = useState<Record<LlmPurpose, string>>(llm.efforts)
  const [tiers, setTiers] = useState<Partial<Record<LlmPurpose, ServiceTier>>>(llm.service_tiers ?? {})
  const [lang, setLang] = useState(llm.output_language ?? '')
  const [auto, setAuto] = useState(llm.outline_enabled)
  const [fx, setFx] = useState(String(llm.usd_to_sgd))
  const [prices, setPrices] = useState<Record<string, ModelPrice>>(llm.prices)
  const [changedPrices, setChangedPrices] = useState<Record<string, ModelPrice | null>>({})
  const [options, setOptions] = useState<string[]>([])
  const [showAll, setShowAll] = useState(false)
  const [listMsg, setListMsg] = useState<string | null>(null)
  const [loadingList, setLoadingList] = useState(false)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)

  const inUse = [...new Set(Object.values(models).map((m) => m.trim()).filter(Boolean))]
  const dirty =
    JSON.stringify(models) !== JSON.stringify(llm.models) ||
    JSON.stringify(efforts) !== JSON.stringify(llm.efforts) ||
    JSON.stringify(tiers) !== JSON.stringify(llm.service_tiers ?? {}) ||
    lang !== (llm.output_language ?? '') ||
    auto !== llm.outline_enabled ||
    Number(fx) !== llm.usd_to_sgd ||
    Object.keys(changedPrices).length > 0
  const shown = showAll ? options : options.filter((m) => !NON_CHAT.test(m))
  const tierOptions = llm.service_tier_options ?? []
  const langOptions = Object.entries(llm.output_language_options ?? {})
  const langChanged = !!llm.output_language && lang !== llm.output_language
  const modelOptions = [...new Set(['deepseek-flash', ...inUse, ...Object.keys(llm.default_prices), ...shown])]

  const fetchModels = async () => {
    setLoadingList(true)
    setListMsg(null)
    try {
      const r = await listModels()
      setOptions(r.models)
      setListMsg(r.error ?? tr("获取到 {0} 个模型，输入框里可以下拉选择", r.models.length))
    } catch (e) {
      setListMsg(errorText(e))
    } finally {
      setLoadingList(false)
    }
  }

  // 后端按整条替换价格：在原记录上改一个字段，其余字段（flex_*、long_* 等）原样带上
  const setPrice = (model: string, field: keyof ModelPrice, value: string) => {
    const base: ModelPrice = prices[model] ?? { input: 0, output: 0 }
    const next = { ...base, [field]: value === '' ? undefined : Number(value) } as ModelPrice
    setPrices({ ...prices, [model]: next })
    setChangedPrices({ ...changedPrices, [model]: next })
  }

  const resetPrice = (model: string) => {
    const def = llm.default_prices[model]
    const next = { ...prices }
    if (def) next[model] = def
    else delete next[model]
    setPrices(next)
    setChangedPrices({ ...changedPrices, [model]: null })
  }

  const save = async () => {
    const rate = Number(fx)
    if (!(rate > 0)) {
      setMsg({ ok: false, text: tr("汇率需要是正数") })
      return
    }
    setBusy(true)
    setMsg(null)
    try {
      const trimmed = Object.fromEntries(Object.entries(models).map(([k, v]) => [k, v.trim()]))
      const body: LlmUpdate = {
        models: trimmed,
        efforts,
        outline_enabled: auto,
        usd_to_sgd: rate,
        prices: changedPrices,
      }
      if (llm.service_tiers) body.service_tiers = tiers
      if (llm.output_language && lang) body.output_language = lang
      const r = await saveLlmSettings(body)
      onSaved(r.llm)
      setModels(r.llm.models)
      setEfforts(r.llm.efforts)
      setTiers(r.llm.service_tiers ?? {})
      setLang(r.llm.output_language ?? '')
      setPrices(r.llm.prices)
      setChangedPrices({})
      setMsg({
        ok: true,
        text: langChanged ? tr("已保存。已生成的大纲需要重新生成（视频页会提示）；问答的新回答直接使用新语言") : tr("已保存"),
      })
    } catch (e) {
      setMsg({ ok: false, text: errorText(e) })
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="settings-models">
      <div hidden={section !== 'outline'}>
      <div className="panel-head">
        <h2>{tr("幻灯片与大纲")}</h2>
      </div>
      <p className="muted small">{tr("分别选择读图和生成大纲的模型，再点击「保存模型设置」。")}</p>
      <p className={`small ${hasDeepseekKey ? 'ok-text' : 'muted'}`}>
        {hasDeepseekKey ? tr("DeepSeek Key 已配置，可以直接选择 DeepSeek Flash。") : tr("使用 DeepSeek Flash 前，请在下方填写 API Key。")}
      </p>
      <button className="btn btn-small" disabled={!hasDeepseekKey || busy} onClick={() => {
        setModels({ ...models, read_slides: 'deepseek-flash', outline: 'deepseek-flash' })
        setEfforts({ ...efforts, read_slides: 'low', outline: 'high' })
        setTiers({ ...tiers, read_slides: 'standard', outline: 'standard' })
        setMsg(null)
      }}>{tr("两项均使用 DeepSeek Flash")}</button>
      </div>
      <div hidden={section !== 'qa'}>
        <div className="panel-head"><h2>{tr("API 问答模型")}</h2></div>
        <p className="muted small">{tr("仅在问答服务选择「API 模型」时使用。Codex 模型在对话输入框中选择。")}</p>
      </div>
      <div className="panel-body settings-body">

        <div hidden={section !== 'advanced'}>
        <h2>{tr("模型列表")}</h2>
        <div className="settings-actions">
          <button className="btn btn-small" onClick={() => void fetchModels()} disabled={!hasKey || loadingList}>
            {loadingList ? tr("获取中…") : tr("获取 OpenAI 兼容接口的模型列表")}
          </button>
          {options.length > 0 && (
            <label className="check small">
              <input type="checkbox" checked={showAll} onChange={(e) => setShowAll(e.target.checked)} />{tr("显示全部（含语音、画图等非对话模型）")}</label>
          )}
          {!hasKey && <span className="muted small">{tr("先填写 OpenAI 兼容接口的 API Key")}</span>}
          {listMsg && <span className="muted small">{systemText(listMsg)}</span>}
        </div>
        </div>
        <datalist id="model-options">
          {shown.map((m) => (
            <option key={m} value={m} />
          ))}
        </datalist>

        {llm.purposes.slice().sort((a, b) => a.id === 'outline' ? -1 : b.id === 'outline' ? 1 : 0).map((p) => (
          <div key={p.id} hidden={section !== (p.id === 'qa' ? 'qa' : 'outline')}>
          <div className="field settings-model-field">
            <label htmlFor={`model-${p.id}`}>{tr(p.label)}{tr("的模型")}</label>
            <select id={`model-${p.id}`} value={models[p.id] ?? ''} disabled={busy}
              onChange={(e) => {
                const model = e.target.value
                setModels({ ...models, [p.id]: model })
                if (model === 'deepseek-flash') {
                  setEfforts({ ...efforts, [p.id]: p.id === 'outline' ? 'high' : 'low' })
                  setTiers({ ...tiers, [p.id]: 'standard' })
                }
                setMsg(null)
              }}>
              {modelOptions.filter(m => p.id !== 'qa' || m !== 'deepseek-flash').map(m => (
                <option key={m} value={m}>{m === 'deepseek-flash' ? 'DeepSeek Flash' : m}</option>
              ))}
            </select>
            <span className="muted small">{tr("已保存：")}{llm.models[p.id]}{models[p.id] !== llm.models[p.id] ? tr(" · 有未保存的更改") : ''}</span>
            {models[p.id]?.trim() === 'deepseek-flash'
              ? <span className={hasDeepseekKey ? 'muted small' : 'warn-text small'}>{hasDeepseekKey ? tr("使用已配置的 DeepSeek Key") : tr("尚未配置 DeepSeek Key，请在下方填写")}</span>
              : <span className="muted small">{tr("使用 OpenAI 兼容接口 ·")} {hasKey ? tr("Key 已配置") : tr("尚未配置 Key")} <button className="btn-text small" onClick={onOpenConnection}>{tr("配置接口")}</button></span>}
            {!prices[models[p.id]?.trim()] && <span className="warn-text small">{tr("此模型尚未配置价格，费用无法估算。请到「接口与费用」填写。")}</span>}
            <details className="settings-details">
              <summary>{tr("高级参数、自定义模型与单价")}</summary>
              <label className="field">
                <span>{tr("手动输入模型 ID")}</span>
              <input
                list="model-options"
                value={models[p.id] ?? ''}
                onChange={(e) => setModels({ ...models, [p.id]: e.target.value })}
                spellCheck={false}
                placeholder={tr("模型 ID，如 gpt-5.6-sol")}
                aria-label={tr("{0}的自定义模型 ID", tr(p.label))}
              />
              </label>
            <div className="model-row">
              <label className="effort-select small">
                <span className="muted">{tr("思考强度")}</span>
                <select value={efforts[p.id]} onChange={(e) => setEfforts({ ...efforts, [p.id]: e.target.value })}>
                  {llm.effort_options.map((o) => (
                    <option key={o} value={o}>
                      {EFFORT_LABELS[o] ?? o}
                    </option>
                  ))}
                </select>
              </label>
              {tierOptions.length > 0 && (
                <label className="effort-select small">
                  <span className="muted">{tr("处理档位")}</span>
                  <select
                    disabled={models[p.id]?.trim() === 'deepseek-flash'}
                    value={models[p.id]?.trim() === 'deepseek-flash' ? 'standard' : tiers[p.id] ?? ''}
                    onChange={(e) => setTiers({ ...tiers, [p.id]: e.target.value as ServiceTier })}
                  >
                    {tierOptions.map((o) => (
                      <option key={o} value={o}>
                        {TIER_LABELS[o] ?? o}
                      </option>
                    ))}
                  </select>
                </label>
              )}
            </div>
            {models[p.id]?.trim() === 'deepseek-flash' && <p className="muted small">{tr("none 关闭思考，medium / xhigh 按 high 执行，不使用 Flex。")}</p>}
            <span className={`small ${prices[models[p.id]?.trim()] ? 'muted' : 'warn-text'}`}>
              {priceLine(prices[models[p.id]?.trim()], models[p.id]?.trim() === 'deepseek-flash' ? 'standard' : tiers[p.id])}
            </span>
            </details>
          </div>
          </div>
        ))}
        <div hidden={section !== 'outline'}>
        <details className="settings-details">
          <summary>{tr("输出语言与自动生成")}</summary>
        {langOptions.length > 0 && (
          <div className="field">
            <span>{tr("输出语言")}</span>
            <div className="model-row">
              <select className="lang-select" value={lang} onChange={(e) => setLang(e.target.value)} aria-label={tr("输出语言")}>
                {langOptions.map(([code, name]) => (
                  <option key={code} value={code}>
                    {systemText(name)}
                  </option>
                ))}
              </select>
            </div>
            <span className={`small ${langChanged ? 'warn-text' : 'muted'}`}>
              {langChanged
                ? tr("保存后，已生成的大纲需要重新生成才会换成新语言；旧的问答记录不变。")
                : tr("大纲和问答回答使用的语言；读幻灯片等中间结果始终用课程原语言。专有名词保留原文。")}
            </span>
          </div>
        )}

        <label className="check">
          <input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} />{tr("处理新视频时自动生成大纲（调用前仍需确认费用）")}</label>
        </details>
        </div>
        <div hidden={section !== 'advanced'}>
        <details className="settings-details">
          <summary>{tr("思考强度与处理档位说明")}</summary>
        <p className="muted small">{tr("思考强度越高，模型推理用的 token 越多（按输出价计费），费用预估会随之变大；不支持推理的模型会忽略此项。")}</p>
        {tierOptions.length > 0 && (
          <p className="muted small">{tr("处理档位：省钱（Flex）按标准价的一半计费，但响应较慢，适合读幻灯片、生成大纲这类后台任务；问答需要实时，建议用标准。 Flex 资源不足时会自动按标准价重试，不会中断。Flex 只对 OpenAI 官方接口生效，DeepSeek 等兼容接口一律按标准档。")}</p>
        )}
        </details>

        <h2>{tr("费用估算")}</h2>
        <p className="muted small">{tr("调整估算使用的价格和汇率，不会改变服务商的实际收费。")}</p>
        <details className="settings-details">
          <summary>{tr("模型价格表")}</summary>
        <div className="price-table">
          <div className="price-head">{tr("在用模型的价格（美元 / 百万 token；默认值为官方价，标准价")}{llm.prices_as_of}{tr("查询")}{llm.flex_prices_as_of ? tr("、Flex 价 {0} 查询", llm.flex_prices_as_of) : ''}{tr("）")}</div>
          {inUse.map((m) => {
            const p = prices[m]
            const custom = llm.custom_prices.includes(m) || m in changedPrices
            return (
              <div key={m} className="price-row">
                <span className="mono small price-model">{m}</span>
                {PRICE_FIELDS.map((f) => (
                  <label key={f.key} className="price-cell small">
                    <span className="muted">{f.label}</span>
                    <input
                      type="number"
                      min="0"
                      step="0.01"
                      value={p?.[f.key] ?? ''}
                      onChange={(e) => setPrice(m, f.key, e.target.value)}
                    />
                  </label>
                ))}
                {m !== 'deepseek-flash' && <>
                <span className="price-break" aria-hidden />
                <span className="small price-model price-tier" title={tr("Flex 档：官方价为标准价的一半；不填时 Flex 请求按标准价估算")}>{tr("Flex 档")}</span>
                {FLEX_PRICE_FIELDS.map((f) => (
                  <label key={f.key} className="price-cell price-cell-flex small">
                    <span className="muted">{f.label}</span>
                    <input
                      type="number"
                      min="0"
                      step="0.001"
                      value={p?.[f.key] ?? ''}
                      placeholder={tr("未填")}
                      aria-label={`${m} Flex ${f.label}`}
                      onChange={(e) => setPrice(m, f.key, e.target.value)}
                    />
                  </label>
                ))}
                </>}
                {custom && (
                  <button className="btn-text small" onClick={() => resetPrice(m)}>
                    {llm.default_prices[m] ? tr("恢复官方价") : tr("删除")}
                  </button>
                )}
              </div>
            )
          })}
        </div>
        </details>

        <label className="field fx-field">
          <span>{tr("汇率：1 USD = ? SGD")}</span>
          <input type="number" min="0" step="0.0001" value={fx} onChange={(e) => setFx(e.target.value)} />
        </label>
        </div>

        <div className="settings-save" hidden={section === 'translation' && !dirty}>
        <div className="settings-actions">
          <button className="btn btn-primary" onClick={() => void save()} disabled={busy || !dirty}>
            {busy ? tr("保存中…") : tr("保存模型设置")}
          </button>
          {dirty && <span className="muted small">{tr("模型、语言或费用设置有未保存的更改")}</span>}
          {msg && <span role="status" className={`small ${msg.ok ? 'ok-text' : 'error-text'}`}>{systemText(msg.text)}</span>}
        </div>
        </div>
      </div>
    </section>
  )
}
