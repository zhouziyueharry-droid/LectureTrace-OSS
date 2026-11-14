import { systemText, tr, useLocale, setLocale, type Locale } from '../i18n.ts'
import { useEffect, useState } from 'react'
import LlmCard from '../components/LlmCard.tsx'
import CodexCard from '../components/CodexCard.tsx'
import McpCard from '../components/McpCard.tsx'
import {
  errorText,
  getSettings,
  saveSettings,
  testService,
  type ApiKeys,
  type KeyInfo,
  type KeysUpdate,
  type LlmSettings,
  type ServiceTestResult,
  type TranslationProvider,
  type SettingsView,
} from '../api.ts'

type Service = 'deepl' | 'google' | 'openai' | 'deepseek'

const BASE_URL_PRESETS = [
  { name: 'OpenAI', url: 'https://api.openai.com/v1' },
]

function keyStatus(k: KeyInfo): string {
  if (!k.has_key) return tr("未填写")
  return k.source === 'env' ? tr("已配置 {0}（来自 .env）", k.key_hint) : tr("已填写 {0}", k.key_hint)
}

export default function SettingsPage() {
  const locale = useLocale()
  const [section, setSection] = useState<'outline' | 'translation' | 'qa' | 'advanced' | 'mcp'>('outline')
  const [keys, setKeys] = useState<ApiKeys | null>(null)
  const [llm, setLlm] = useState<LlmSettings | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [translator, setTranslator] = useState<TranslationProvider>('deepl')
  const [savingTranslator, setSavingTranslator] = useState(false)
  const [translatorError, setTranslatorError] = useState<string | null>(null)
  const [ollama, setOllama] = useState<SettingsView['ollama'] | null>(null)
  const [localTest, setLocalTest] = useState<ServiceTestResult | null>(null)
  const [testingLocal, setTestingLocal] = useState(false)

  const testLocal = async () => {
    setTestingLocal(true)
    setLocalTest(null)
    try { setLocalTest(await testService('ollama')) }
    catch (error) { setLocalTest({ ok: false, message: errorText(error), latency_ms: 0 }) }
    finally { setTestingLocal(false) }
  }

  const changeTranslator = async (provider: TranslationProvider) => {
    setSavingTranslator(true)
    setTranslatorError(null)
    try {
      const result = await saveSettings({ translator: provider })
      setTranslator(result.translator)
    } catch (error) { setTranslatorError(errorText(error)) }
    finally { setSavingTranslator(false) }
  }

  useEffect(() => {
    getSettings().then(
      (r) => {
        setKeys(r.keys)
        setLlm(r.llm)
        setTranslator(r.translator)
        setOllama(r.ollama)
      },
      (e) => setLoadError(errorText(e)),
    )
  }, [])

  useEffect(() => { document.title = tr('设置 - LectureTrace') }, [locale])

  return (
    <div className="settings-page">
      <header className="vp-head">
        <h1 className="vp-title">{tr("设置")}</h1>
      </header>
      <section className="panel settings-card settings-language">
        <div className="panel-body settings-body">
          <label className="field" htmlFor="interface-language">
            <span>{tr('界面语言')} / {locale === 'en' ? '界面语言' : 'Interface language'}</span>
            <select id="interface-language" value={locale} onChange={event => setLocale(event.target.value as Locale)}>
              <option value="zh-CN" lang="zh-CN">简体中文</option>
              <option value="en" lang="en">English</option>
            </select>
          </label>
          <p className="muted small">{tr('立即应用并自动保存到此浏览器。课程内容和 AI 输出语言不受影响。')}</p>
        </div>
      </section>
      <p className="muted settings-intro">{tr("按用途选择服务和模型。更换模型后，已有课程不会自动重新生成。")}</p>
      <nav className="settings-nav" aria-label={tr("设置分类")}>
        {([
          ['outline', tr("幻灯片与大纲")],
          ['translation', tr("字幕翻译")],
          ['qa', tr("问答")],
          ['advanced', tr("接口与费用")],
          ['mcp', tr('本机 MCP')],
        ] as const).map(([id, label]) => (
          <button key={id} type="button" aria-pressed={section === id}
            onClick={() => setSection(id)}>{label}</button>
        ))}
      </nav>
      {section === 'mcp' && <McpCard />}
      {loadError && <p className="error-text">{tr("加载设置失败：")}{systemText(loadError)}</p>}
      {!keys && !loadError && <p className="muted">{tr("加载中…")}</p>}
      <div hidden={section !== 'qa'}><CodexCard /></div>
      {keys && (
        <>
          <div hidden={section !== 'translation'}>
          <section className="panel settings-card">
            <div className="panel-head"><h2>{tr("字幕翻译")}</h2></div>
            <div className="panel-body settings-body">
              <label className="field">
                <span>{tr("翻译服务")}</span>
                <select value={translator} disabled={savingTranslator}
                  onChange={event => void changeTranslator(event.target.value as TranslationProvider)}>
                  <option value="deepl">DeepL</option>
                  <option value="google">Google Cloud Translation</option>
                  <option value="ollama">{tr("Ollama（本机 HY-MT2，无需 API Key）")}</option>
                  <option value="llm">{tr("LLM（现有模型接口）")}</option>
                </select>
              </label>
              <p className="muted small">{tr("选择后自动保存。不会自动重跑课程；失败的翻译可回到视频页重试，保留已有译文并补全剩余字幕。")}</p>
              {savingTranslator && <p role="status">{tr("保存中…")}</p>}
              {translatorError && <p className="error-text" role="alert">{systemText(translatorError)}</p>}
              {translator === 'ollama' && (
                <>
                  <p className="small">{tr("字幕在本机翻译，无 API 费用。Ollama 需保持运行，可以关闭聊天终端。")}</p>
                  <p className="muted small">{tr("模型：")}{ollama?.model}<br />{tr("接口：")}{ollama?.base_url}</p>
                  <div><button className="btn" disabled={testingLocal} onClick={() => void testLocal()}>
                    {testingLocal ? tr("检查中…") : tr("检查 Ollama 和模型")}
                  </button></div>
                  {localTest && <p role="status" className={localTest.ok ? 'ok-text small' : 'error-text small'}>{systemText(localTest.message)}</p>}
                </>
              )}
              {translator === 'llm' && <p className="muted small">{tr("使用 OpenAI 兼容接口，接口地址与 Key 可在「接口与费用」中设置。")}</p>}
            </div>
          </section>
          {translator === 'google' && <ServiceCard service="google" title={tr("Google Cloud Translation（翻译字幕）")}
            info={keys.google} onSaved={setKeys}
            help={<>{tr("在 Google Cloud 项目启用 Cloud Translation API、关联结算账号并创建 API Key（限制为 Cloud Translation API）。使用 Basic v2 的 NMT 翻译为简体中文。费用按未扣免费抵扣的标准价估算，余额以 Google Cloud 为准。测试连接只查询支持语言，不发送字幕。")}</>} />}
          {translator === 'deepl' && <ServiceCard
            service="deepl"
            title={tr("DeepL（翻译字幕）")}
            info={keys.deepl}
            onSaved={setKeys}
            help={
              <>{tr("在 DeepL 官网注册 API 账号后，在账户页面复制 Key。免费版 Key 以")}<code>:fx</code>{tr("结尾，每月有免费字符额度（具体以 DeepL 官网为准），发送前会显示本月已用多少。")}</>
            }
          />}
          </div>
          <div hidden={section !== 'advanced'}>
          <ServiceCard
            service="openai"
            title={tr("OpenAI 兼容接口")}
            info={keys.openai}
            baseUrl={keys.openai.base_url}
            onSaved={setKeys}
            help={<>{tr("供 OpenAI 兼容模型及 LLM 字幕翻译共用。DeepSeek Flash 使用独立 Key，不需要更改这里的地址。")}</>}
          />
          </div>
          <div hidden={section === 'mcp'}>{llm && <LlmCard section={section === 'mcp' ? 'advanced' : section} llm={llm} hasKey={keys.openai.has_key}
            hasDeepseekKey={keys.deepseek?.has_key} onSaved={setLlm}
            onOpenConnection={() => setSection('advanced')} />}</div>
          <div hidden={section !== 'outline'}>
          <ServiceCard service="deepseek" title="DeepSeek API Key"
            info={keys.deepseek ?? { has_key: false, key_hint: null, source: null }} onSaved={setKeys}
            help={<>{tr("独立接口 https://api.deepseek.com。保存 Key 后，在上方选择 DeepSeek Flash 并保存模型设置。测试连接只查询模型列表，不发送课程内容。")}</>} />
          </div>
          <details className="settings-details settings-storage">
            <summary>{tr("密钥保存位置")}</summary>
            <p className="muted small">{tr("API Key 只保存在本机后端的数据库里，页面上只显示末 4 位。设置页没填时，会读取项目根目录 .env 里的同名配置。")}</p>
          </details>
        </>
      )}
    </div>
  )
}

interface CardProps {
  service: Service
  title: string
  info: KeyInfo
  baseUrl?: string
  help: React.ReactNode
  onSaved: (keys: ApiKeys) => void
}

function ServiceCard({ service, title, info, baseUrl, help, onSaved }: CardProps) {
  useLocale()
  const [key, setKey] = useState('')
  const [url, setUrl] = useState(baseUrl ?? '')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)
  const [test, setTest] = useState<ServiceTestResult | null>(null)
  const [testing, setTesting] = useState(false)

  const dirty = key.trim() !== '' || (baseUrl !== undefined && url.trim() !== baseUrl)

  const save = async (update: KeysUpdate) => {
    setBusy(true)
    setError(null)
    setSaved(false)
    setTest(null)
    try {
      const r = await saveSettings(update)
      onSaved(r.keys)
      setKey('')
      setSaved(true)
    } catch (e) {
      setError(errorText(e))
    } finally {
      setBusy(false)
    }
  }

  const onSave = () => {
    const update: KeysUpdate = {}
    if (key.trim()) update[service] = key.trim()
    if (baseUrl !== undefined && url.trim() !== baseUrl) update.openai_base_url = url.trim()
    void save(update)
  }

  const runTest = async () => {
    setTesting(true)
    setTest(null)
    try {
      setTest(await testService(service))
    } catch (e) {
      setTest({ ok: false, message: errorText(e), latency_ms: 0 })
    } finally {
      setTesting(false)
    }
  }

  return (
    <details className="settings-details settings-service" open={info.has_key ? undefined : true}>
      <summary>
        <span>{title}</span>
        <span className="muted small">{keyStatus(info)}</span>
      </summary>
      <div className="panel-body settings-body">
        <label className="field">
          <span>API Key</span>
          <input
            type="password"
            autoComplete="off"
            placeholder={info.has_key ? tr("留空表示不修改") : tr("粘贴 API Key")}
            value={key}
            onChange={(e) => {
              setKey(e.target.value)
              setSaved(false)
            }}
          />
        </label>
        {baseUrl !== undefined && (
          <label className="field">
            <span>{tr("接口地址")}</span>
            <input value={url} onChange={(e) => setUrl(e.target.value)} spellCheck={false} />
            <span className="field-presets">
              {BASE_URL_PRESETS.map((p) => (
                <button key={p.name} type="button" className="btn btn-small" onClick={() => setUrl(p.url)}>
                  {p.name}
                </button>
              ))}
            </span>
          </label>
        )}
        <div className="settings-actions">
          <button className="btn btn-primary" onClick={onSave} disabled={busy || !dirty}>
            {busy ? tr("保存中…") : tr("保存")}
          </button>
          <button className="btn" onClick={() => void runTest()} disabled={testing || busy || !info.has_key}>
            {testing ? tr("测试中…") : tr("测试连接")}
          </button>
          {info.has_key && info.source === 'settings' && (
            <button className="btn btn-ghost-danger" onClick={() => void save({ [service]: '' })} disabled={busy}>{tr("清除 Key")}</button>
          )}
          {saved && <span className="ok-text small">{tr("已保存")}</span>}
        </div>
        <details className="settings-details">
          <summary>{tr("配置说明")}</summary>
          <p className="muted small">{help}</p>
        </details>
        {error && <p className="error-text small">{systemText(error)}</p>}
        {test && (
          <p className={`small ${test.ok ? 'ok-text' : 'error-text'}`}>
            {test.ok ? '✓ ' : '✗ '}
            {systemText(test.message)}
            {test.latency_ms > 0 && <span className="muted">{tr("（")}{test.latency_ms}{tr("ms）")}</span>}
          </p>
        )}
      </div>
    </details>
  )
}
