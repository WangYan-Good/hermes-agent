import { describe, expect, it } from 'vitest'

import { parseMouseTrackingOverride } from '../config/env.js'

describe('parseMouseTrackingOverride', () => {
  it('preserves explicit mouse-tracking presets from the process environment', () => {
    expect(parseMouseTrackingOverride('wheel')).toBe('wheel')
    expect(parseMouseTrackingOverride('buttons')).toBe('buttons')
    expect(parseMouseTrackingOverride('all')).toBe('all')
    expect(parseMouseTrackingOverride('off')).toBe('off')
  })
})
