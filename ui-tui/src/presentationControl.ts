import { WebSocket } from 'undici'

interface ViewState {
  sid: string | null
  blocked: boolean
  draft: boolean
}
interface Authority {
  ready?: boolean
  ticket?: string
  released?: boolean
  stored_id?: string | null
}
let readView: (() => ViewState) | null = null
const listeners = new Set<() => void>()
export let presentationFrozen = false
let observedBytes = 0

export function setPresentationView(read: (() => ViewState) | null) {
  readView = read
  listeners.forEach(fn => fn())
}

/** Runs in Ink, so both gateway topologies query the actual session owner. */
export function connectPresentationControl(
  request: <T>(method: string, params: Record<string, unknown>) => Promise<T>
) {
  const url = process.env.HERMES_TUI_PRESENTATION_URL

  if (!url) {
    return () => {}
  }

  let ws: WebSocket
  let stopped = false
  let retry: ReturnType<typeof setTimeout> | undefined
  let ticket: string | null = null
  let pending = false

  const input = (data: Buffer | string) => {
    observedBytes += Buffer.byteLength(data)
  }

  process.stdin.on('data', input)

  const changed = () => {
    if (ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ changed: true }))
    }
  }

  const connect = () => {
    ws = new WebSocket(url)
    const connection = ws

    ws.addEventListener('open', changed)
    ws.addEventListener('close', () => {
      if (!stopped) {
        retry = setTimeout(connect, 1000)
      }
    })
    ws.addEventListener('message', event => {
      void (async () => {
        let frame: {
          handoff?: boolean
          id: string
          action: string
          input_bytes: number
          profile: string
          generation: string
        }

        try {
          frame = JSON.parse(String(event.data)) as typeof frame
        } catch {
          return
        }

        if (!frame.handoff || pending) {
          return
        }

        pending = true

        const reply = (payload: object) => {
          if (connection.readyState === WebSocket.OPEN) {
            connection.send(
              JSON.stringify({ id: frame.id, profile: frame.profile, generation: frame.generation, ...payload })
            )
          }
        }

        try {
          // Control WS and PTY stdin are independent streams. Fence bytes already
          // accepted by the server before sampling the actual Ink input owner.
          if (frame.input_bytes > observedBytes) {
            await new Promise<void>((resolve, reject) => {
              const check = () => {
                if (observedBytes >= frame.input_bytes) {
                  cleanup()
                  resolve()
                }
              }

              const timeout = setTimeout(() => {
                cleanup()
                reject(new Error('Input not drained'))
              }, 3000)

              const cleanup = () => {
                clearTimeout(timeout)
                process.stdin.off('data', check)
              }

              process.stdin.on('data', check)
            })
          }

          // Finish all listeners of that stdin event (composer refs + submit
          // admission) before reading the view, without inspecting its text.
          await new Promise<void>(resolve => setImmediate(resolve))
          const view = readView?.()

          if (!view?.sid) {
            throw new Error('TUI not ready')
          }

          const action = frame.action

          if (action === 'prepare' && (view.blocked || view.draft)) {
            reply({ result: { ready: false, draft: view.draft } })

            return
          }

          if (action === 'prepare') {
            presentationFrozen = true
          }

          // Recover an ACK-lost ticket through the same authoritative owner.
          if (action === 'cancel') {
            const status = await request<Authority>('session.handoff', { session_id: view.sid, action: 'status' })

            ticket = status.ticket ?? null

            if (!ticket) {
              presentationFrozen = false
              reply({ result: { cancelled: true } })

              return
            }
          }

          const result = await request<Authority>('session.handoff', { session_id: view.sid, action, ticket })

          if (result.ticket) {
            ticket = result.ticket
          }

          if (action === 'cancel' || (action === 'prepare' && !result.ready)) {
            presentationFrozen = false
          }

          reply({
            result:
              action === 'status'
                ? {
                    ...result,
                    confirmed: !!result.ready,
                    ready: result.ready && !view.blocked && !view.draft,
                    draft: view.draft
                  }
                : result
          })
        } catch {
          reply({ error: 'Handoff unavailable' })
        } finally {
          pending = false
        }
      })()
    })
  }

  connect()
  listeners.add(changed)

  return () => {
    stopped = true
    clearTimeout(retry)
    listeners.delete(changed)
    process.stdin.off('data', input)
    ws.close()
  }
}

export function presentationChanged() {
  listeners.forEach(fn => fn())
}
