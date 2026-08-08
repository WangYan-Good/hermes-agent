import type { GatewayEvent } from '../gatewayTypes.js'
import type { ApprovalReq, ClarifyReq, SecretReq, SudoReq } from '../types.js'

import { getOverlayState, patchOverlayState } from './overlayStore.js'

export interface ControlPromptSource {
  sessionId: string
  sessionTitle: string
}

export type ControlPrompt =
  | { kind: 'approval'; request: ApprovalReq }
  | { kind: 'clarify'; request: ClarifyReq }
  | { kind: 'secret'; request: SecretReq }
  | { kind: 'sudo'; request: SudoReq }

type ControlKind = ControlPrompt['kind']
type ControlResult = { kind: 'expire'; promptKind: 'clarify' | 'secret' | 'sudo'; requestId: string } | { kind: 'request'; prompt: ControlPrompt }

const current = (): ControlPrompt | null => {
  const state = getOverlayState()

  if (state.approval) {return { kind: 'approval', request: state.approval }}

  if (state.clarify) {return { kind: 'clarify', request: state.clarify }}

  if (state.secret) {return { kind: 'secret', request: state.secret }}

  if (state.sudo) {return { kind: 'sudo', request: state.sudo }}

  return null
}

const idOf = (prompt: ControlPrompt) => ('requestId' in prompt.request ? prompt.request.requestId : undefined)

const matches = (prompt: ControlPrompt, kind: ControlKind, requestId?: string) =>
  prompt.kind === kind && (requestId === undefined || idOf(prompt) === requestId)

const promote = () => {
  const state = getOverlayState()

  if (current() || state.controlQueue.length === 0) {return}
  const [next, ...controlQueue] = state.controlQueue

  if (next) {patchOverlayState({ [next.kind]: next.request, controlQueue })}
}

export const enqueueControlPrompt = (prompt: ControlPrompt) => {
  if (current()) {
    patchOverlayState(state => ({ ...state, controlQueue: [...state.controlQueue, prompt] }))

    return
  }

  patchOverlayState({ [prompt.kind]: prompt.request })
}

export const completeControlPrompt = (kind: ControlKind, requestId?: string) => {
  const active = current()

  if (active && matches(active, kind, requestId)) {
    patchOverlayState({ [kind]: null })
    promote()

    return
  }

  patchOverlayState(state => ({ ...state, controlQueue: state.controlQueue.filter(prompt => !matches(prompt, kind, requestId)) }))
}

export const expireControlPrompt = (kind: 'clarify' | 'secret' | 'sudo', requestId: string) => completeControlPrompt(kind, requestId)

export const controlPromptFromEvent = (
  event: GatewayEvent,
  currentSessionId: null | string,
  titleForSession: (sessionId: string) => string
): ControlResult | null => {
  const sessionId = event.session_id ?? currentSessionId ?? 'default'
  const sessionTitle = titleForSession(sessionId) || sessionId

  switch (event.type) {
    case 'approval.request':
      return { kind: 'request', prompt: { kind: 'approval', request: { allowPermanent: event.payload.allow_permanent !== false, choices: event.payload.choices, command: String(event.payload.command ?? ''), description: String(event.payload.description ?? 'dangerous command'), sessionId, sessionTitle, smartDenied: event.payload.smart_denied === true } } }

    case 'clarify.request':
      return { kind: 'request', prompt: { kind: 'clarify', request: { choices: event.payload.choices, question: event.payload.question, requestId: event.payload.request_id, sessionId, sessionTitle } } }

    case 'secret.request':
      return { kind: 'request', prompt: { kind: 'secret', request: { envVar: event.payload.env_var, prompt: event.payload.prompt, requestId: event.payload.request_id, sessionId, sessionTitle } } }

    case 'sudo.request':
      return { kind: 'request', prompt: { kind: 'sudo', request: { requestId: event.payload.request_id, sessionId, sessionTitle } } }

    case 'clarify.expire':
      return { kind: 'expire', promptKind: 'clarify', requestId: event.payload.request_id }

    case 'secret.expire':
      return { kind: 'expire', promptKind: 'secret', requestId: event.payload.request_id }

    case 'sudo.expire':
      return { kind: 'expire', promptKind: 'sudo', requestId: event.payload.request_id }

    default:
      return null
  }
}
