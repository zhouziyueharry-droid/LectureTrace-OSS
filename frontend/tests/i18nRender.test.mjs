import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { registerHooks } from 'node:module'
import ts from 'typescript'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { setLocale } from '../src/i18n.ts'

// Use the installed compiler to render the actual TSX components; no extra test runtime.
registerHooks({ load(url, context, nextLoad) {
  if (url.endsWith('.tsx')) return { format: 'module', shortCircuit: true, source: ts.transpileModule(readFileSync(new URL(url), 'utf8'), {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext, jsx: ts.JsxEmit.ReactJSX },
  }).outputText }
  return nextLoad(url, context)
} })
const load = async name => (await import(`../src/${name}.tsx`)).default
const [App, Settings, LlmCard, Player, Outline, StagePanel, BlockEditor, ImportDialog, PackageImportDialog] = await Promise.all([
  'App', 'pages/SettingsPage', 'components/LlmCard', 'components/Player', 'components/OutlinePanel', 'components/StagePanel', 'notes/BlockEditor', 'components/ImportDialog', 'components/PackageImportDialog',
].map(load))
const noop = () => {}
const { default: McpCard, McpStatusDetails } = await import('../src/components/McpCard.tsx')
const llm = { provider:'openai', models:{read_slides:'demo',outline:'demo',qa:'demo'}, efforts:{read_slides:'low',outline:'low',qa:'low'}, service_tiers:{}, effort_options:['low'], service_tier_options:['standard'], output_language:'zh-CN', output_language_options:{'zh-CN':'简体中文',en:'English'}, outline_enabled:true, usd_to_sgd:1.3, prices:{demo:{input:1,output:2}}, default_prices:{demo:{input:1,output:2}}, custom_prices:[], prices_as_of:'2026-11-13', purposes:[{id:'read_slides',label:'读取幻灯片'}] }
const video={id:'demo',title:'保留课程名',status:'failed',roi_confirmed:true,roi:null,stale_stages:[],stages:[{name:'asr',status:'failed',error:'视频文件不存在',attempt:1}]}
const cases = [
  ['mcp',McpCard,{},'Checking MCP','正在检查 MCP'],
  ['settings',Settings,{},'Interface language','界面语言'],
  ['models',LlmCard,{section:'outline',llm,hasKey:false,onSaved:noop,onOpenConnection:noop},'Read slides model','读取幻灯片'],
  ['player',Player,{videoId:'demo',videoRef:{current:null},subsVersion:null,subsHint:'字幕尚未生成',onTime:noop,videoH:360,videoHMax:800,onVideoH:noop},'Video controls','视频控制'],
  ['outline',Outline,{outline:{chapters:[{title:'保留原标题',summary:'保留原文',start:0,evidence:[]}],admin:null,speech_categories:{},warnings:[]},loading:false,error:null,placeholder:'',currentTime:0,onSeek:noop},'Outline','大纲'],
  ['stages',StagePanel,{video,live:{},conn:'open',retrying:false,retryError:null,onRetry:noop,onOpenPlan:noop,onRunOutline:noop,onEditRoi:noop,startOpen:true},'Video file not found','视频文件不存在'],
  ['notes',BlockEditor,{text:'保留原文',onChange:noop,citations:[],stale:false,onSeek:noop,getTime:()=>0},'Paragraph menu','段落菜单'],
  ['import',ImportDialog,{initialFiles:[],initialCourseId:null,courses:[],onClose:noop,onDone:noop,onCourseCreated:noop,notify:noop},'Import MP4','导入 MP4'],
  ['package',PackageImportDialog,{onClose:noop,onDone:noop,notify:noop,modes:()=>null},'Import course package','导入课程包'],
]

test('MCP states and failures render in both languages without claiming client connectivity', () => {
  const states = {
    ready: ['MCP HTTP is ready', 'MCP HTTP 可用'],
    disabled: ['MCP HTTP is disabled', 'MCP HTTP 未启用'],
    missing_dependency: ['MCP dependency is not installed', '未安装 MCP 依赖'],
    starting: ['MCP HTTP has not started', 'MCP HTTP 尚未启动'],
    error: ['MCP check failed', 'MCP 检查失败'],
  }
  try {
    for (const locale of ['en', 'zh-CN']) {
      setLocale(locale)
      for (const [state, labels] of Object.entries(states)) {
        const status = { state, token_configured: state !== 'disabled', endpoint: '/mcp',
          tools: state === 'ready' ? [{name:'list_videos',description:'Read library'}] : [],
          library_readable: state === 'ready' ? true : null, protocol_version: state === 'ready' ? '2025-06-18' : null,
          error_code: state === 'error' ? 'library_unreadable' : null, checked_at:'2026-11-13T00:00:00Z', latency_ms:12 }
        const html = render(createElement(McpStatusDetails, {status}))
        assert.ok(html.includes(labels[locale === 'en' ? 0 : 1]))
        if (state === 'ready') assert.ok(html.includes('list_videos'))
        if (state === 'disabled') assert.ok(html.includes('LECTURETRACE_MCP_TOKEN'))
        if (locale === 'en') assert.doesNotMatch(html, /[\u3400-\u9fff]/u)
      }
      const html = render(createElement(McpCard))
      assert.match(html, /disabled=""/)
      assert.ok(html.includes(locale === 'en' ? 'It does not verify external clients' : '不代表外部客户端'))
    }
  } finally { setLocale('zh-CN') }
})
function render(element) {
  const previous = console.error
  console.error = (...args) => {
    // Client-only hooks are not executed by this markup test.
    if (String(args[0]).includes('useLayoutEffect does nothing on the server')) return
    previous(...args)
  }
  try { return renderToStaticMarkup(element) } finally { console.error = previous }
}

test('real components render localized controls while preserving course and note content', () => {
  for (const locale of ['en','zh-CN']) {
    setLocale(locale)
    for (const [name, Component, props, en, zh] of cases) {
      const router=createMemoryRouter([{path:'*',element:createElement(Component,props)}])
      const html=render(createElement(RouterProvider,{router}))
      router.dispose()
      assert.ok(html.includes(locale==='en'?en:zh), `${name} missing ${locale} copy`)
      if(locale==='en'&&name!=='settings') assert.doesNotMatch(html.replaceAll('保留原文','').replaceAll('保留原标题','').replaceAll('保留课程名',''), /[\u3400-\u9fff]/u, `${name} contains untranslated controls`)
      if (name==='notes'||name==='outline') assert.ok(html.includes('保留原文'), `${name} rewrote source content`)
      if(locale==='en'&&name==='models') assert.ok(html.includes('Simplified Chinese'))
      if(locale==='en'&&name==='settings') assert.match(html, /value="en"[^>]* selected=""/)
    }
  }
  setLocale('zh-CN')
})

test('all application routes render in both languages without locale-dependent keys', () => {
  globalThis.window={innerWidth:1440,innerHeight:900,matchMedia:()=>({matches:false})}
  try {
    for(const locale of ['en','zh-CN']) {
      setLocale(locale)
      for(const path of ['/','/settings','/inbox','/courses/demo','/videos/demo','/videos/demo/keyframes','/missing']) {
        const router=createMemoryRouter([{path:'*',element:createElement(App)}],{initialEntries:[path]})
        const html=render(createElement(RouterProvider,{router}))
        assert.ok(html.length>50, `${path} rendered empty in ${locale}`)
        if(path==='/missing')assert.ok(html.includes(locale==='en'?'Page not found.':'页面不存在。'))
        router.dispose()
      }
    }
  } finally {delete globalThis.window;setLocale('zh-CN')}
})
