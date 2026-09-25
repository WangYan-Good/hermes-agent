import { createContext, useContext } from 'react'

import type { ChatHostAdapter } from './contracts'

export const ChatHostContext = createContext<ChatHostAdapter | null>(null)
export const useChatHost = () => useContext(ChatHostContext)
