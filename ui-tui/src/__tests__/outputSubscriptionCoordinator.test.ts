import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  createOutputSubscriptionCoordinator,
  getOutputSubscriptionState,
  resetOutputSubscriptionState
} from '../app/outputSubscriptionCoordinator.js'
import type { SessionActiveItem } from '../gatewayTypes.js'

const sessions = (): SessionActiveItem[] => [
  { id: 'sid-a', owned: true, session_key: 'stored-a', status: 'working', title: 'Alpha', watchable: false },
  { id: 'sid-b', owned: false, session_key: 'stored-b', status: 'working', title: 'Beta', watchable: true },
  { id: 'sid-c', owned: false, session_key: 'stored-c', status: 'idle', title: 'Gamma', watchable: true }
]

describe('output subscription coordinator', () => {
  beforeEach(() => resetOutputSubscriptionState())

  it('keeps desired watches by durable key and effective subscriptions by runtime sid', async () => {
    const rpc = vi.fn(async (_method: string, params: Record<string, unknown>) => ({
      rejected: [],
      subscriptions: [{ session_id: (params.session_ids as string[])[0] }]
    }))

    const coordinator = createOutputSubscriptionCoordinator(rpc)

    coordinator.syncActiveSessions(sessions(), 'sid-a')
    await coordinator.watch('sid-b')

    expect(rpc).toHaveBeenCalledWith('session.output_subscribe', {
      session_ids: ['sid-b'],
      subscriber_session_id: 'sid-a'
    })
    expect(getOutputSubscriptionState()).toMatchObject({
      desired: { 'stored-b': { runtimeSessionId: 'sid-b', status: 'subscribed' } },
      effective: { 'sid-b': { anchorSessionId: 'sid-a', sessionKey: 'stored-b' } }
    })
  })

  it('resubscribes desired durable keys after gateway generation and runtime remap', async () => {
    const rpc = vi.fn(async (_method: string, params: Record<string, unknown>) => ({
      rejected: [],
      subscriptions: [{ session_id: (params.session_ids as string[])[0] }]
    }))

    const coordinator = createOutputSubscriptionCoordinator(rpc)

    coordinator.syncActiveSessions(sessions(), 'sid-a')
    await coordinator.watch('sid-b')
    rpc.mockClear()

    coordinator.gatewayReady()
    coordinator.syncActiveSessions(
      sessions().map(item => (item.session_key === 'stored-b' ? { ...item, id: 'sid-b-remapped' } : item)),
      'sid-a'
    )
    await coordinator.settled()

    expect(rpc).toHaveBeenCalledWith('session.output_subscribe', {
      session_ids: ['sid-b-remapped'],
      subscriber_session_id: 'sid-a'
    })
    expect(getOutputSubscriptionState().effective).toEqual({
      'sid-b-remapped': { anchorSessionId: 'sid-a', sessionKey: 'stored-b' }
    })
  })

  it('records rejected watch state without treating it as effective', async () => {
    const rpc = vi.fn(async () => ({
      rejected: [{ reason: 'hidden', session_id: 'sid-b' }],
      subscriptions: []
    }))

    const coordinator = createOutputSubscriptionCoordinator(rpc)

    coordinator.syncActiveSessions(sessions(), 'sid-a')
    expect(await coordinator.watch('sid-b')).toBe(false)

    expect(getOutputSubscriptionState()).toMatchObject({
      desired: { 'stored-b': { reason: 'hidden', status: 'rejected' } },
      effective: {}
    })
  })

  it('unwatches the effective runtime subscription and removes durable desire', async () => {
    const rpc = vi.fn(async (method: string) =>
      method === 'session.output_subscribe'
        ? { rejected: [], subscriptions: [{ session_id: 'sid-b' }] }
        : { unsubscribed: ['sid-b'] }
    )

    const coordinator = createOutputSubscriptionCoordinator(rpc)

    coordinator.syncActiveSessions(sessions(), 'sid-a')
    await coordinator.watch('sid-b')
    expect(await coordinator.unwatch('sid-b')).toBe(true)

    expect(rpc).toHaveBeenLastCalledWith('session.output_unsubscribe', {
      session_ids: ['sid-b'],
      subscriber_session_id: 'sid-a'
    })
    expect(getOutputSubscriptionState().desired).toEqual({})
    expect(getOutputSubscriptionState().effective).toEqual({})
  })

  it('focuses watched output without activating it and marks owner loss read-only', async () => {
    const coordinator = createOutputSubscriptionCoordinator(
      vi.fn(async () => ({ rejected: [], subscriptions: [{ session_id: 'sid-b' }] }))
    )

    const activate = vi.fn().mockResolvedValue(true)

    coordinator.syncActiveSessions(sessions(), 'sid-a')
    await coordinator.watch('sid-b')
    expect(await coordinator.focus('sid-b', 'sid-a', activate)).toBe(true)
    expect(getOutputSubscriptionState().focusedSessionId).toBe('sid-b')
    expect(activate).not.toHaveBeenCalled()
    expect(coordinator.roleFor('sid-a')).toBe('Owned')
    expect(coordinator.roleFor('sid-b')).toBe('Watched')

    coordinator.ownerLost('sid-a')
    expect(getOutputSubscriptionState().focusedSessionId).toBe('sid-a')
    expect(coordinator.roleFor('sid-a')).toBe('Inactive')
  })

  it('supports multiple simultaneous desired watches', async () => {
    const rpc = vi.fn(async (_method: string, params: Record<string, unknown>) => ({
      rejected: [],
      subscriptions: [{ session_id: (params.session_ids as string[])[0] }]
    }))

    const coordinator = createOutputSubscriptionCoordinator(rpc)

    coordinator.syncActiveSessions(sessions(), 'sid-a')
    await coordinator.watch('sid-b')
    await coordinator.watch('sid-c')

    expect(Object.keys(getOutputSubscriptionState().desired).sort()).toEqual(['stored-b', 'stored-c'])
    expect(Object.keys(getOutputSubscriptionState().effective).sort()).toEqual(['sid-b', 'sid-c'])
  })

  it('selects the owned anchor whose profile authorizes the target', async () => {
    const rpc = vi.fn(async (_method: string, params: Record<string, unknown>) =>
      params.subscriber_session_id === 'sid-profile-2'
        ? { rejected: [], subscriptions: [{ session_id: 'sid-b' }] }
        : { rejected: [{ reason: 'profile_mismatch', session_id: 'sid-b' }], subscriptions: [] }
    )

    const coordinator = createOutputSubscriptionCoordinator(rpc)

    coordinator.syncActiveSessions(
      [
        ...sessions(),
        { id: 'sid-profile-2', owned: true, session_key: 'stored-profile-2', status: 'idle', watchable: false }
      ],
      'sid-a'
    )
    expect(await coordinator.watch('sid-b')).toBe(true)

    expect(rpc).toHaveBeenNthCalledWith(1, 'session.output_subscribe', {
      session_ids: ['sid-b'],
      subscriber_session_id: 'sid-a'
    })
    expect(rpc).toHaveBeenNthCalledWith(2, 'session.output_subscribe', {
      session_ids: ['sid-b'],
      subscriber_session_id: 'sid-profile-2'
    })
    expect(getOutputSubscriptionState().effective['sid-b']).toEqual({
      anchorSessionId: 'sid-profile-2',
      sessionKey: 'stored-b'
    })
  })

  it('never reuses a controlled session as anchor after owner loss', async () => {
    const rpc = vi.fn(async () => ({ rejected: [], subscriptions: [{ session_id: 'sid-c' }] }))
    const coordinator = createOutputSubscriptionCoordinator(rpc)

    coordinator.syncActiveSessions(
      [
        { id: 'sid-a', owned: false, status: 'working', watchable: true },
        { id: 'sid-b', owned: true, status: 'working', watchable: false },
        { id: 'sid-c', owned: false, status: 'working', watchable: true }
      ],
      'sid-a'
    )
    expect(await coordinator.watch('sid-c')).toBe(true)
    expect(rpc).toHaveBeenCalledWith('session.output_subscribe', {
      session_ids: ['sid-c'],
      subscriber_session_id: 'sid-b'
    })
  })

  it('removes the target watch after Take Control without dropping other owned sessions', async () => {
    const rpc = vi.fn(async () => ({ rejected: [], subscriptions: [{ session_id: 'sid-b' }] }))
    const coordinator = createOutputSubscriptionCoordinator(rpc)

    coordinator.syncActiveSessions(sessions(), 'sid-a')
    await coordinator.watch('sid-b')
    await coordinator.takeControlSucceeded('sid-b')

    expect(coordinator.isOwned('sid-a')).toBe(true)
    expect(coordinator.isOwned('sid-b')).toBe(true)
    expect(coordinator.isDesired('sid-b')).toBe(false)
    expect(coordinator.isWatched('sid-b')).toBe(false)
    expect(rpc).toHaveBeenLastCalledWith('session.output_unsubscribe', {
      session_ids: ['sid-b'],
      subscriber_session_id: 'sid-a'
    })
  })
})
