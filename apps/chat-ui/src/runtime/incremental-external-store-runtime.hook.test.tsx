import {
  type AssistantRuntime,
  AssistantRuntimeProvider,
  type ExternalStoreAdapter,
  fromThreadMessageLike,
  type ThreadMessage,
  useAssistantRuntime,
  useAuiState
} from '@assistant-ui/react'
import { cleanup, render, renderHook, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { useIncrementalExternalStoreRuntime } from '../index'

afterEach(cleanup)

function message(id: string, role: 'assistant' | 'user', text: string): ThreadMessage {
  return fromThreadMessageLike({ role, content: [{ type: 'text', text }] }, id, {
    type: 'complete',
    reason: 'stop'
  })
}

interface TranscriptProps {
  expectedRuntime: AssistantRuntime
}

interface SurfaceProps {
  store: ExternalStoreAdapter<ThreadMessage>
}

function adapter(messages: ThreadMessage[], isRunning = false): ExternalStoreAdapter<ThreadMessage> {
  return {
    messageRepository: {
      headId: messages.at(-1)?.id ?? null,
      messages: messages.map((item, index) => ({
        message: item,
        parentId: messages[index - 1]?.id ?? null
      }))
    },
    isRunning,
    onNew: async () => {}
  }
}

describe('shared incremental runtime hook', () => {
  it('preserves runtime and settled message identity through a streamed tail update', () => {
    const user = message('user', 'user', 'hello')
    const first = message('reply', 'assistant', 'hel')
    const initial = adapter([user, first], true)
    const { result, rerender } = renderHook(useIncrementalExternalStoreRuntime, { initialProps: initial })
    const runtime = result.current
    const messages = runtime.thread.getState().messages

    rerender({ ...initial })
    expect(result.current).toBe(runtime)
    expect(runtime.thread.getState().messages).toBe(messages)

    const tail = message('reply', 'assistant', 'hello back')
    rerender(adapter([user, tail], true))
    expect(result.current).toBe(runtime)
    expect(runtime.thread.getState().messages[0]).toBe(user)
    expect(runtime.thread.getState().messages[1]).toBe(tail)
  })

  it('keeps the optimistic assistant ephemeral and replaces it with the authoritative reply', () => {
    const user = message('user', 'user', 'hello')

    const { result, rerender } = renderHook(useIncrementalExternalStoreRuntime, {
      initialProps: adapter([user], true)
    })

    const placeholder = result.current.thread.getState().messages.at(-1)!

    expect(placeholder.role).toBe('assistant')
    expect(placeholder.metadata.isOptimistic).toBe(true)

    expect(result.current.thread.export().messages.map(item => item.message.id)).toEqual(['user'])

    const reply = message('reply', 'assistant', 'hello back')
    rerender(adapter([user, reply], true))
    expect(result.current.thread.getState().messages).toEqual([user, reply])

    expect(result.current.thread.export().messages.map(item => item.message.id)).toEqual(['user', 'reply'])
  })

  it('emits run transitions once and removes a pending placeholder on return to idle', () => {
    const user = message('user', 'user', 'hello')
    const initial = adapter([user])
    const { result, rerender } = renderHook(useIncrementalExternalStoreRuntime, { initialProps: initial })
    const start = vi.fn()
    const end = vi.fn()
    const unsubscribeStart = result.current.thread.unstable_on('runStart', start)
    const unsubscribeEnd = result.current.thread.unstable_on('runEnd', end)

    rerender({ ...initial, isRunning: true })
    expect(result.current.thread.getState().isRunning).toBe(true)
    expect(result.current.thread.getState().messages).toHaveLength(2)
    rerender({ ...initial, isRunning: true })
    expect(start).toHaveBeenCalledTimes(1)

    rerender(initial)
    expect(result.current.thread.getState().isRunning).toBe(false)
    expect(result.current.thread.getState().messages).toEqual([user])
    expect(end).toHaveBeenCalledTimes(1)
    rerender({ ...initial })
    expect(end).toHaveBeenCalledTimes(1)
    unsubscribeStart()
    unsubscribeEnd()
  })

  it('reconciles capabilities even when the repository and running state stay unchanged', () => {
    const initial = adapter([message('reply', 'assistant', 'done')])
    const { result, rerender } = renderHook(useIncrementalExternalStoreRuntime, { initialProps: initial })
    const messages = result.current.thread.getState().messages

    expect(result.current.thread.getState().capabilities).toMatchObject({ edit: false, cancel: false, reload: false })
    rerender({
      ...initial,
      onEdit: async () => {},
      onCancel: async () => {},
      onReload: async () => {},
      setMessages: () => {},
      unstable_capabilities: { copy: false }
    })
    expect(result.current.thread.getState().capabilities).toMatchObject({
      edit: true,
      cancel: true,
      reload: true,
      switchToBranch: true,
      unstable_copy: false
    })
    expect(result.current.thread.getState().messages).toBe(messages)

    rerender(initial)
    expect(result.current.thread.getState().capabilities).toMatchObject({
      edit: false,
      cancel: false,
      reload: false,
      switchToBranch: false,
      unstable_copy: true
    })
  })

  it('isolates mounted runtimes and clears the old transcript and placeholder on a disjoint switch', () => {
    const initial = adapter([message('old', 'user', 'old session')], true)
    const foreground = renderHook(useIncrementalExternalStoreRuntime, { initialProps: initial })
    const backgroundMessage = message('background', 'assistant', 'background session')

    const background = renderHook(useIncrementalExternalStoreRuntime, {
      initialProps: adapter([backgroundMessage])
    })

    const backgroundMessages = background.result.current.thread.getState().messages

    const next = message('new', 'assistant', 'new session')

    foreground.rerender(adapter([next]))
    expect(foreground.result.current.thread.getState().messages).toEqual([next])
    expect(foreground.result.current.thread.export().messages.map(item => item.message.id)).toEqual(['new'])
    expect(background.result.current.thread.getState().messages).toBe(backgroundMessages)
    expect(background.result.current.thread.getState().messages[0]).toBe(backgroundMessage)
  })

  it('shares the consumer provider context while rendering, streaming and switching transcripts', async () => {
    function Transcript({ expectedRuntime }: TranscriptProps) {
      const runtime = useAssistantRuntime()
      const messages = useAuiState(state => state.thread.messages)

      expect(runtime).toBe(expectedRuntime)

      return <div>{messages.flatMap(item => item.content.map(part => part.type === 'text' ? part.text : '')).join(' ')}</div>
    }

    function Surface({ store }: SurfaceProps) {
      const runtime = useIncrementalExternalStoreRuntime(store)

      return <AssistantRuntimeProvider runtime={runtime}><Transcript expectedRuntime={runtime} /></AssistantRuntimeProvider>
    }

    const user = message('user', 'user', 'question')
    const view = render(<Surface store={adapter([user, message('reply', 'assistant', 'partial')], true)} />)
    expect(await screen.findByText('question partial')).toBeTruthy()
    view.rerender(<Surface store={adapter([user, message('reply', 'assistant', 'finished')])} />)
    expect(await screen.findByText('question finished')).toBeTruthy()
    view.rerender(<Surface store={adapter([message('other', 'assistant', 'another session')])} />)
    expect(await screen.findByText('another session')).toBeTruthy()
    expect(screen.queryByText('question finished')).toBeNull()
  })
})
