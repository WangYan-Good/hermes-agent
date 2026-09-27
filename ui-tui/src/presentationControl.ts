import { atom } from 'nanostores'
import { WebSocket } from 'undici'

export interface PresentationView {
  sid: string | null
  blocked: string[]
}
interface ControlFrame {
  type: string
  id: string
  instance: string
  generation: string
  action: string
  input_bytes: number
  ticket?: string
}
interface Authority {
  confirmed?: boolean
  ready?: boolean
  blocked?: string[]
  ticket?: string
  released?: boolean
  cancelled?: boolean
}

let readView: (() => PresentationView) | null = null
export const $presentationFrozen = atom(false)
const listeners = new Set<() => void>()

export function setPresentationView(read: (() => PresentationView) | null) {
  readView = read
  listeners.forEach(listener => listener())
}

/** Owner state is sampled after the stdin fence, never inferred from elapsed time. */
export async function handlePresentationControl(
  frame: ControlFrame,
  observed: () => number,
  request: <T>(method: string, params: Record<string, unknown>) => Promise<T>
): Promise<Authority> {
  if (frame.action === 'prepare' || frame.action === 'release') {
    $presentationFrozen.set(true)
  }

  if (frame.input_bytes > observed()) {
    await new Promise<void>((resolve, reject) => {
      const check = () => {
        if (observed() >= frame.input_bytes) {
          clearTimeout(timer)
          process.stdin.off('data', check)
          resolve()
        }
      }

      const timer = setTimeout(() => {
        process.stdin.off('data', check)
        reject(new Error('Terminal input fence not confirmed'))
      }, 3000)

      process.stdin.on('data', check)
      check()
    })
  }

  await new Promise<void>(resolve => setImmediate(resolve))
  const view = readView?.()

  if (!view?.sid) {
    throw new Error('Terminal not ready')
  }

  if (['prepare', 'release'].includes(frame.action) && view.blocked.length) {
    return { confirmed: true, ready: false, blocked: view.blocked }
  }

  const result = await request<Authority>('terminal.presentation', {
    action: frame.action,
    session_id: view.sid,
    ticket: frame.ticket
  })

  if (frame.action === 'cancel' && result.cancelled) {
    $presentationFrozen.set(false)
  }

  return frame.action === 'status'
    ? {
        ...result,
        ready: result.ready && view.blocked.length === 0,
        blocked: [...(result.blocked ?? []), ...view.blocked]
      }
    : result
}

export function connectPresentationControl(
  request: <T>(method: string, params: Record<string, unknown>) => Promise<T>
) {
  const url = process.env.HERMES_TUI_PRESENTATION_URL

  if (!url) {
    return () => {}
  }
  const instance = new URL(url).searchParams.get('instance')!
  let observedBytes = 0
  let stopped = false
  let retry: ReturnType<typeof setTimeout> | undefined
  let ws: WebSocket | undefined
  let chain = Promise.resolve()

  const observe = (data: Buffer | string) => {
    observedBytes += Buffer.byteLength(data)
  }
  process.stdin.on('data', observe)

  const changed = () => {
    if (ws?.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'changed', instance }))
    }
  }

  const connect = () => {
    const socket = new WebSocket(url, ['hermes.pty-control.v1'])
    ws = socket
    socket.addEventListener('open', changed)
    socket.addEventListener('close', () => {
      $presentationFrozen.set(true)

      if (!stopped) {
        retry = setTimeout(connect, 1000)
      }
    })
    socket.addEventListener('message', event => {
      chain = chain.then(async () => {
        let frame: ControlFrame

        try {
          frame = JSON.parse(String(event.data)) as ControlFrame
        } catch {
          return
        }

        if (frame.type !== 'control' || frame.instance !== instance) {
          return
        }

        const reply = (payload: object) => {
          if (socket.readyState === WebSocket.OPEN) {
            socket.send(JSON.stringify({ id: frame.id, instance, generation: frame.generation, ...payload }))
          }
        }

        try {
          reply({ result: await handlePresentationControl(frame, () => observedBytes, request) })
        } catch {
          reply({ error: 'Terminal owner could not confirm lifecycle' })
        }
      })
    })
  }

  connect()
  listeners.add(changed)

  return () => {
    stopped = true
    clearTimeout(retry)
    listeners.delete(changed)
    process.stdin.off('data', observe)
    ws?.close()
  }
}
