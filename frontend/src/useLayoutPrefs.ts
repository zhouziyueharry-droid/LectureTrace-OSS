import { useCallback, useEffect, useState } from 'react'

/** Agent 停在视频下方时的页面区宽度，以及右侧 Agent 侧栏宽度 */
export const OUTLINE_W = { min: 280, max: 900, def: 480 }
export const CHAT_W = { min: 300, max: 640, def: 400 }
export const VIDEO_H_MIN = 200
/** 视频列的保底宽度：拖动或打开 Agent 都不能把视频挤到比这更窄 */
export const VIDEO_MIN_W = 480
/** 竖向分隔条占的宽度 */
export const SPLIT_W = 12

export type ChatDock = 'side' | 'below'
/** 悬浮小窗：左上角位置与宽度（px），高度按 16:9 加控制条跟随 */
export interface FloatBox { x: number; y: number; w: number }

const KEY = 'lt.videoLayout.v5'
const OLD_KEY = 'lt.videoLayout.v4'

interface Layout {
  outlineW: number
  chatW: number
  /** Agent 是否展开 */
  chatOpen: boolean
  /** Agent 放在右侧还是视频下方 */
  chatDock: ChatDock
  /** 视频画面的最大高度；null 表示自适应 */
  videoH: number | null
  /** 视频是否浮成页面内小窗；浮起后空出的列给页面区 */
  videoFloat: boolean
  videoHidden: boolean
  /** 小窗的位置和宽度；null 时放右下角 */
  floatBox: FloatBox | null
}

const DEFAULT: Layout = { outlineW: OUTLINE_W.def, chatW: CHAT_W.def, chatOpen: false, chatDock: 'side', videoH: null, videoFloat: false, videoHidden: false, floatBox: null }

const validBox = (b: unknown): b is FloatBox =>
  !!b && typeof b === 'object' && ['x', 'y', 'w'].every((k) => Number.isFinite((b as Record<string, unknown>)[k]))

export const clamp = (v: number, min: number, max: number) => Math.max(min, Math.min(max, v))

function load(): Layout {
  try {
    const raw = localStorage.getItem(KEY) ?? localStorage.getItem(OLD_KEY)
    if (!raw) return DEFAULT
    const p = JSON.parse(raw) as Partial<Layout>
    return {
      outlineW: clamp(Number(p.outlineW) || OUTLINE_W.def, OUTLINE_W.min, OUTLINE_W.max),
      chatW: clamp(Number(p.chatW) || CHAT_W.def, CHAT_W.min, CHAT_W.max),
      chatOpen: p.chatOpen === true,
      chatDock: p.chatDock === 'below' ? 'below' : 'side',
      videoH: typeof p.videoH === 'number' ? Math.max(VIDEO_H_MIN, p.videoH) : null,
      videoFloat: p.videoFloat === true,
      videoHidden: p.videoHidden === true,
      floatBox: validBox(p.floatBox) ? p.floatBox : null,
    }
  } catch {
    return DEFAULT
  }
}

export interface ResolvedLayout {
  /** stack：窄屏纵向堆叠；columns：桌面网格 */
  arrangement: 'stack' | 'columns'
  outlineW: number
  chatW: number
  chatPlace: 'side' | 'below' | 'hidden'
  /** 用户选了右侧，但当前宽度放不下，临时放到了视频下方 */
  autoBelow: boolean
  /** 当前宽度下页面区和 Agent 能拖到的最大宽度（保证视频不窄于 VIDEO_MIN_W） */
  outlineMax: number
  chatMax: number
  /** 视频浮成小窗：网格里只剩页面区和 Agent */
  floating: boolean
}

/** 按网格的实际宽度算出有效尺寸；只算不存，窗口变大后回到用户拖出来的尺寸 */
export function resolveLayout(l: Layout, gridW: number): ResolvedLayout {
  const resolved = resolveColumns(l, gridW)
  return { ...resolved, floating: l.videoFloat }
}

function resolveColumns(l: Layout, gridW: number): Omit<ResolvedLayout, 'floating'> {
  const hidden = !l.chatOpen
  if (gridW < VIDEO_MIN_W + SPLIT_W + CHAT_W.min) {
    return { arrangement: 'stack', outlineW: l.outlineW, chatW: l.chatW, chatPlace: hidden ? 'hidden' : 'below', autoBelow: false, outlineMax: OUTLINE_W.max, chatMax: CHAT_W.max }
  }
  if (l.videoFloat || l.videoHidden || hidden || l.chatDock === 'side') {
    // 默认视频在页面区上方，共用左列；浮窗时页面区占满左列。
    const leftMin = l.videoFloat || l.videoHidden ? OUTLINE_W.min : VIDEO_MIN_W
    const chatMax = Math.max(CHAT_W.min, Math.min(CHAT_W.max, gridW - leftMin - SPLIT_W))
    return { arrangement: 'columns', outlineW: l.outlineW, chatW: clamp(l.chatW, CHAT_W.min, chatMax), chatPlace: hidden ? 'hidden' : 'side', autoBelow: false, outlineMax: OUTLINE_W.max, chatMax }
  }
  const room = gridW - SPLIT_W - VIDEO_MIN_W
  const outlineW = clamp(l.outlineW, OUTLINE_W.min, Math.min(OUTLINE_W.max, room))
  return { arrangement: 'columns', outlineW, chatW: l.chatW, chatPlace: 'below', autoBelow: false, outlineMax: Math.min(OUTLINE_W.max, room), chatMax: CHAT_W.max }
}

/** 视频页的面板尺寸与 Agent 开关、位置，保存在本机浏览器 */
export function useLayoutPrefs() {
  const [layout, setLayout] = useState<Layout>(load)

  useEffect(() => {
    try {
      localStorage.setItem(KEY, JSON.stringify(layout))
    } catch {
      /* 存储不可用 */
    }
  }, [layout])

  const toggleChat = useCallback(() => {
    setLayout((l) => ({ ...l, chatOpen: !l.chatOpen }))
  }, [])
  const openChat = useCallback(() => {
    setLayout((l) => (l.chatOpen ? l : { ...l, chatOpen: true }))
  }, [])
  const closeChat = useCallback(() => {
    setLayout((l) => (l.chatOpen ? { ...l, chatOpen: false } : l))
  }, [])
  const setChatDock = useCallback((chatDock: ChatDock) => {
    setLayout((l) => ({ ...l, chatDock }))
  }, [])
  /** max：当前宽度下允许的最大值，由调用方按 resolveLayout 传入 */
  const setOutlineW = useCallback((w: number, max: number = OUTLINE_W.max) => {
    setLayout((l) => ({ ...l, outlineW: clamp(w, OUTLINE_W.min, Math.max(OUTLINE_W.min, max)) }))
  }, [])
  const setChatW = useCallback((w: number, max: number = CHAT_W.max) => {
    setLayout((l) => ({ ...l, chatW: clamp(w, CHAT_W.min, Math.max(CHAT_W.min, max)) }))
  }, [])
  const setVideoH = useCallback((h: number | null) => {
    setLayout((l) => ({ ...l, videoH: h === null ? null : Math.max(VIDEO_H_MIN, h) }))
  }, [])
  const toggleFloat = useCallback(() => {
    setLayout((l) => ({ ...l, videoFloat: !l.videoFloat }))
  }, [])
  const setFloatBox = useCallback((floatBox: FloatBox) => {
    setLayout((l) => ({ ...l, floatBox }))
  }, [])
  const setVideoHidden = useCallback((videoHidden: boolean) => {
    setLayout((l) => ({ ...l, videoHidden }))
  }, [])

  return { ...layout, toggleChat, openChat, closeChat, setChatDock, setOutlineW, setChatW, setVideoH, toggleFloat, setFloatBox, setVideoHidden }
}
