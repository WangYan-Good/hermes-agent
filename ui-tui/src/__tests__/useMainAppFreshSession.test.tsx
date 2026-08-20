import { EventEmitter } from 'node:events'
import { PassThrough } from 'node:stream'

import { renderSync } from '@hermes/ink'
import { useStore } from '@nanostores/react'
import React, { useLayoutEffect } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { DOMElement, DOMNode } from '../../packages/hermes-ink/src/ink/dom.js'
import instances from '../../packages/hermes-ink/src/ink/instances.js'
import { GatewayProvider } from '../app/gatewayContext.js'
import { resetOutputStreams } from '../app/outputStreamStore.js'
import { resetOverlayState } from '../app/overlayStore.js'
import { turnController } from '../app/turnController.js'
import { resetTurnState } from '../app/turnStore.js'
import { $uiState, getUiState, patchUiState, resetUiState } from '../app/uiStore.js'
import { useMainApp } from '../app/useMainApp.js'
import { AppLayout } from '../components/appLayout.js'
import type * as EnvModule from '../config/env.js'
import type { GatewayClient } from '../gatewayClient.js'

vi.mock('../config/env.js', async importActual => {
  const actual = await importActual<typeof EnvModule>()

  return {
    ...actual,
    DASHBOARD_TUI_MODE: true,
    INLINE_MODE: false,
    NO_CONFIRM_DESTRUCTIVE: true,
    STARTUP_IMAGE: '',
    STARTUP_QUERY: '',
    STARTUP_RESUME_ID: ''
  }
})

// Keep useMainApp, AppLayout, AlternateScreen, and the renderer on one source
// module graph. The package import otherwise resolves through the built copy,
// whose instance registry cannot expose the production tree rendered here.
vi.mock('@hermes/ink', async () => import('../../packages/hermes-ink/src/entry-exports.js'))

type MainAppModel = ReturnType<typeof useMainApp>

const count = (value: string, sequence: string) => value.split(sequence).length - 1

const flushPromises = async () => {
  for (let index = 0; index < 8; index += 1) {
    await Promise.resolve()
  }

  // React schedules passive effects outside the promise microtask queue.
  // Waiting for the Node check phase keeps this harness on the same lifecycle
  // as the real renderer instead of observing state before useEffect runs.
  await new Promise<void>(resolve => setImmediate(resolve))
  await new Promise<void>(resolve => setImmediate(resolve))
}

const nodeText = (node: DOMNode): string =>
  node.nodeName === '#text' ? node.nodeValue : node.childNodes.map(child => nodeText(child)).join('')

const findElement = (root: DOMElement, predicate: (node: DOMElement) => boolean): DOMElement | null => {
  if (predicate(root)) {
    return root
  }

  for (const child of root.childNodes) {
    if (child.nodeName !== '#text') {
      const found = findElement(child, predicate)

      if (found) {
        return found
      }
    }
  }

  return null
}

const topWithin = (node: DOMElement, ancestor: DOMElement) => {
  let current: DOMElement | undefined = node
  let top = 0

  while (current !== ancestor) {
    top += current.yogaNode?.getComputedTop() ?? 0
    current = current.parentNode

    if (!current) {
      throw new Error('node is not inside the expected alternate-screen root')
    }
  }

  return top
}

const makeGateway = () => {
  const gateway = new EventEmitter() as EventEmitter & Record<string, unknown>
  const sessions = ['session-a', 'session-b']

  gateway.request = vi.fn(async (method: string) => {
    if (method === 'setup.status') {
      return { provider_configured: true }
    }

    if (method === 'session.create') {
      const sessionId = sessions.shift()

      return sessionId ? { session_id: sessionId, stored_session_id: `stored-${sessionId}` } : null
    }

    if (method === 'session.active_list') {
      const sid = getUiState().sid

      return { sessions: sid ? [{ current: true, id: sid, status: 'idle', title: sid }] : [] }
    }

    if (method === 'session.close') {
      return { status: 'closed' }
    }

    return {}
  })
  gateway.drain = vi.fn()
  gateway.getLogTail = vi.fn(() => '')
  gateway.kill = vi.fn()
  gateway.publishLocalEvent = vi.fn()
  gateway.send = vi.fn()
  gateway.start = vi.fn()

  return gateway as unknown as GatewayClient
}

function MainAppHarness({ expose, gw }: { expose: React.MutableRefObject<MainAppModel | null>; gw: GatewayClient }) {
  const model = useMainApp(gw)
  const { mouseTracking } = useStore($uiState)

  useLayoutEffect(() => {
    expose.current = model
  }, [expose, model])

  return (
    <GatewayProvider value={model.gateway}>
      <AppLayout
        actions={model.appActions}
        composer={model.appComposer}
        dashboardMode
        mouseTracking={mouseTracking}
        progress={model.appProgress}
        status={model.appStatus}
        transcript={model.appTranscript}
      />
    </GatewayProvider>
  )
}

describe('Dashboard fresh-session rendering', () => {
  beforeEach(() => {
    resetOutputStreams()
    resetOverlayState()
    resetTurnState()
    resetUiState()
    turnController.fullReset()
    patchUiState({ mouseTracking: 'wheel' })
  })

  it('uses the mounted alternate screen for fresh sessions without destructive terminal recovery', async () => {
    const stdout = process.stdout
    const stdin = new PassThrough()
    const stderr = new PassThrough()
    let output = ''

    Object.assign(stdin, { isTTY: true, ref: () => {}, setRawMode: () => {}, unref: () => {} })
    Object.assign(stderr, { isTTY: false })

    const stdoutDescriptors = new Map(
      ['columns', 'isTTY', 'rows'].map(key => [key, Object.getOwnPropertyDescriptor(stdout, key)] as const)
    )

    Object.defineProperties(stdout, {
      columns: { configurable: true, value: 169 },
      isTTY: { configurable: true, value: true },
      rows: { configurable: true, value: 45 }
    })

    const writeSpy = vi.spyOn(stdout, 'write').mockImplementation(((chunk: string | Uint8Array, ...args: unknown[]) => {
      output += typeof chunk === 'string' ? chunk : Buffer.from(chunk).toString('utf8')
      const callback = args.find(value => typeof value === 'function') as undefined | ((error?: Error | null) => void)

      callback?.()

      return true
    }) as typeof stdout.write)

    const expose = { current: null as MainAppModel | null }
    const gw = makeGateway()
    const renderApp = () => <MainAppHarness expose={expose} gw={gw} />
    let app: null | ReturnType<typeof renderSync> = null

    try {
      app = renderSync(renderApp(), {
        patchConsole: false,
        stderr: stderr as NodeJS.WriteStream,
        stdin: stdin as NodeJS.ReadStream,
        stdout
      })

      const ink = instances.get(stdout) as unknown as { rootNode: DOMElement }

      expect(count(output, '\x1b[?1049h')).toBe(1)

      expose.current!.appComposer.submit('/new')
      await flushPromises()
      app.rerender(renderApp())
      await flushPromises()

      expect(getUiState().sid).toBe('session-a')
      expect(count(output, '\x1b[?1049h')).toBe(1)

      expose.current!.appComposer.submit('SESSION_A_UNIQUE_TEXT')
      await flushPromises()
      app.rerender(renderApp())
      await flushPromises()

      expect(nodeText(ink.rootNode)).toContain('SESSION_A_UNIQUE_TEXT')
      patchUiState({ busy: false, status: 'ready' })

      const beforeTransition = {
        enter: count(output, '\x1b[?1049h'),
        erase: count(output, '\x1b[2J'),
        exit: count(output, '\x1b[?1049l')
      }

      expose.current!.appComposer.submit('/new')
      await flushPromises()
      app.rerender(renderApp())
      await flushPromises()

      const finalText = nodeText(ink.rootNode)
      const screenRoot = ink.rootNode.childNodes[0] as DOMElement

      const composer = findElement(
        screenRoot,
        node => node.parentNode === screenRoot.childNodes[0] && node.style.noSelect === 'from-left-edge'
      )

      expect(getUiState().sid).toBe('session-b')
      expect(count(output, '\x1b[?1049h') - beforeTransition.enter).toBe(0)
      expect(count(output, '\x1b[?1049l') - beforeTransition.exit).toBe(0)
      expect(count(output, '\x1b[2J') - beforeTransition.erase).toBe(0)
      expect(finalText).not.toContain('SESSION_A_UNIQUE_TEXT')
      expect(finalText).toContain('new session started')
      expect(composer).not.toBeNull()

      const composerTop = topWithin(composer!, screenRoot)
      const composerHeight = composer!.yogaNode?.getComputedHeight() ?? 0

      expect(screenRoot.yogaNode?.getComputedHeight()).toBe(45)
      expect(composerTop + composerHeight).toBeLessThanOrEqual(45)
    } finally {
      app?.unmount()
      app?.cleanup()
      writeSpy.mockRestore()

      for (const [key, descriptor] of stdoutDescriptors) {
        if (descriptor) {
          Object.defineProperty(stdout, key, descriptor)
        } else {
          delete (stdout as unknown as Record<string, unknown>)[key]
        }
      }
    }
  })
})
