import { afterEach, expect, it, vi } from 'vitest';
import { createBrowserUuid } from './browser-uuid';

const uuidV4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
afterEach(() => vi.unstubAllGlobals());

it('prefers randomUUID and preserves its Crypto receiver', () => {
  const secureCrypto = {
    randomUUID: vi.fn(() => 'caa8eb30-6a47-4262-963d-c7d845dca84e'),
    getRandomValues: vi.fn(),
  };
  vi.stubGlobal('crypto', secureCrypto);
  expect(createBrowserUuid()).toMatch(uuidV4);
  expect(secureCrypto.randomUUID).toHaveBeenCalledOnce();
  expect(secureCrypto.randomUUID.mock.contexts[0]).toBe(secureCrypto);
  expect(secureCrypto.getRandomValues).not.toHaveBeenCalled();
});

it('uses secure entropy for distinct v4 UUIDs on insecure HTTP', () => {
  const getRandomValues = vi.fn(crypto.getRandomValues.bind(crypto));
  vi.stubGlobal('isSecureContext', false);
  vi.stubGlobal('crypto', { getRandomValues });
  const ids = Array.from({ length: 32 }, () => createBrowserUuid());
  for (const id of ids) expect(id).toMatch(uuidV4);
  expect(new Set(ids).size).toBe(ids.length);
  expect(getRandomValues).toHaveBeenCalledTimes(ids.length);
  expect(getRandomValues.mock.calls.every(([bytes]) => bytes instanceof Uint8Array && bytes.byteLength === 16)).toBe(true);
});

it.each([
  [0x00, '00000000-0000-4000-8000-000000000000'],
  [0xff, 'ffffffff-ffff-4fff-bfff-ffffffffffff'],
])('sets only the version and variant bits for entropy byte %i', (byte, expected) => {
  const secureCrypto = {
    getRandomValues(bytes: Uint8Array) {
      expect(this).toBe(secureCrypto);
      bytes.fill(byte);
      return bytes;
    },
  };
  vi.stubGlobal('crypto', secureCrypto);
  expect(createBrowserUuid()).toBe(expected);
});

it.each([undefined, {}])('fails explicitly without a secure entropy source (%s)', value => {
  vi.stubGlobal('crypto', value);
  expect(() => createBrowserUuid()).toThrow(/secure.*(?:random|entropy)/i);
});
