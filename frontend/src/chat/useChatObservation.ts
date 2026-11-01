import { useEffect, useMemo, useRef, useSyncExternalStore } from 'react'
import { askStream, getChat, getChatRun, getChatContext, listChatConversations, stopChat } from '../api.ts'
import { createChatObservation } from './observation.ts'

const transport = { askStream, getChat, getChatRun, getChatContext, listChatConversations, stopChat }

export function useChatObservation(videoId: string, onNotesChanged?: () => void) {
  const onNotesChangedRef = useRef(onNotesChanged)
  onNotesChangedRef.current = onNotesChanged
  const observer = useMemo(() => createChatObservation(videoId, transport, () => onNotesChangedRef.current?.()), [videoId])
  const state = useSyncExternalStore(observer.subscribe, observer.getSnapshot)
  useEffect(() => {
    void observer.reload()
    return observer.disconnect
  }, [observer])
  return { observer, ...state }
}
