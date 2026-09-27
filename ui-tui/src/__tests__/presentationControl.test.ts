import { afterEach, expect, it, vi } from 'vitest'

import { handlePresentationControl, setPresentationView } from '../presentationControl.js'

const frame = (action: string) => ({ type: 'control', id: 'id', instance: 'instance', generation: 'generation', input_bytes: 0, action })
afterEach(() => setPresentationView(null))
it.each(['turn', 'queue', 'interaction', 'composer'])('actual %s state blocks prepare and release', async blocked => {
  const request = vi.fn()
  setPresentationView(() => ({ sid: 'runtime', blocked: [blocked] }))

  for (const action of ['prepare', 'release']) {
    expect(await handlePresentationControl(frame(action), () => 0, request)).toMatchObject({ ready: false, blocked: [blocked] })
  }

  expect(request).not.toHaveBeenCalled()
})
it('samples live state after the byte fence and does not parse terminal text', async () => {
  let observed = 0
  let blocked: string[] = []
  const request = vi.fn()
  setPresentationView(() => ({ sid: 'runtime', blocked }))
  const pending = handlePresentationControl({ ...frame('prepare'), input_bytes: 3 }, () => observed, request)
  blocked = ['composer']
  observed = 3
  process.stdin.emit('data', Buffer.from('abc'))
  expect(await pending).toMatchObject({ ready: false, blocked: ['composer'] })
  expect(request).not.toHaveBeenCalled()
})
it('cancel requires the actual gateway ACK and release forwards its ticket', async () => {
  setPresentationView(() => ({ sid: 'runtime', blocked: [] }))
  const request = vi.fn().mockResolvedValue({ cancelled: true })
  expect(await handlePresentationControl(frame('cancel'), () => 0, request)).toEqual({ cancelled: true })
  await handlePresentationControl({ ...frame('release'), ticket: 'authority' }, () => 0, request)
  expect(request).toHaveBeenLastCalledWith('terminal.presentation', { action: 'release', session_id: 'runtime', ticket: 'authority' })
})
it('missing owner view never reports ready', async () => {
  await expect(handlePresentationControl(frame('status'), () => 0, vi.fn())).rejects.toThrow('not ready')
})
