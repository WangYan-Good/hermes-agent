// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from 'vitest';
import * as attachments from './native-attachments';

beforeEach(() => window.sessionStorage.clear());

describe('attachment occurrences', () => {
  it('rejects a late completion after removal and after a scope change', async () => {
    expect(typeof attachments.NativeAttachments).toBe('function');
    let scope = { runtimeId: 'r1', generation: 1, profile: '' };
    let finish!: (value: Response) => void;
    const client = new attachments.NativeAttachments(() => scope, async (method, params) => {
      if (method === 'attachment.prepare') return { draft_id: 'draft', draft_token: 'grant', attachment: params.occurrence_id ? { id: 'a1', occurrence_id: params.occurrence_id, request_id: params.upload_request_id, name: 'a.txt', size: 1, mime: 'text/plain', state: 'local' } : null };
      return { connection_token: 'connection', attachments: [] };
    }, async (url) => url.endsWith('/recover') ? new Response(JSON.stringify({ draft_id: 'draft', draft_token: 'grant', attachments: [] })) : new Promise(resolve => { finish = resolve; }));
    const pending = client.add([new File(['x'], 'a.txt', { type: 'text/plain' }), new File(['y'], 'b.txt', { type: 'text/plain' })]);
    for (let i = 0; i < 20 && !finish; i++) await new Promise(resolve => setTimeout(resolve, 0));
    const id = client.getSnapshot().items[0].occurrence_id;
    await client.remove(id);
    scope = { runtimeId: 'r2', generation: 2, profile: '' };
    finish(new Response(JSON.stringify({ id: 'a1', occurrence_id: id, state: 'uploaded' })));
    await pending;
    expect(client.getSnapshot().items.filter(a => a.state === 'uploaded')).toHaveLength(0);
    expect(client.getSnapshot().items.some(a => a.name === 'b.txt')).toBe(false);
  });
});

function harness() {
  const scope = { runtimeId: 'r', generation: 1, profile: '' };
  const ledger = new Map<string, attachments.NativeAttachment>();
  let serial = 0;
  const rpc = vi.fn(async (method: string, p: Record<string, unknown>) => {
    if (method === 'attachment.prepare') {
      let item;
      if (p.occurrence_id) { item = { id: `a${++serial}`, occurrence_id: p.occurrence_id as string, request_id: p.upload_request_id as string, name: p.name as string, size: p.size as number, mime: p.mime as string, state: 'local' as const }; ledger.set(item.id, item); }
      return { draft_id: 'd', draft_token: 'memory-only', attachment: item };
    }
    if (method === 'attachment.cancel') { const item = ledger.get(p.attachment_id as string)!; ledger.set(item.id!, { ...item, state: 'cancelled' }); }
    return { connection_token: 'connection', attachments: [...ledger.values()] };
  });
  const http = vi.fn(async (url: string) => {
    if (url.endsWith('/recover')) return new Response(JSON.stringify({ draft_id: 'd', draft_token: 'rotated', attachments: [...ledger.values()] }));
    const id = url.split('/').at(-1)!; const item = { ...ledger.get(id)!, state: 'uploaded' as const }; ledger.set(id, item); return new Response(JSON.stringify(item));
  });
  const client = new attachments.NativeAttachments(() => scope, rpc, http);
  return { client, ledger, rpc, http, scope };
}
it('uses the snapshot after a lost upload response, without uploading twice', async () => {
  const { client, http, ledger } = harness();
  const normal = http.getMockImplementation()!;
  http.mockImplementation(async url => { const result = await normal(url); if (!url.endsWith('/recover')) throw new Error('response lost'); return result; });
  await client.add([new File(['bytes'], 'a.txt', { type: 'text/plain' })]);
  expect(client.getSnapshot().items[0].state).toBe('uploaded');
  expect(http.mock.calls.filter(([url]) => !url.endsWith('/recover'))).toHaveLength(1);
  expect(client.submitPayload().attachment_ids).toEqual([...ledger.keys()]);
});
it('keeps unknown submit acceptance locked, then recovers a claimed attachment without replay', async () => {
  const { client, ledger, rpc } = harness();
  await client.add([new File(['bytes'], 'a.txt', { type: 'text/plain' })]);
  const item = client.getSnapshot().items[0];
  client.markUncertain();
  expect(() => client.submitPayload()).toThrow();
  await client.remove(item.occurrence_id);
  expect(rpc.mock.calls.some(([method]) => method === 'attachment.cancel')).toBe(false);
  ledger.set(item.id!, { ...item, state: 'submitted' });
  client.invalidate(); await client.recover();
  expect(client.getSnapshot().items[0].state).toBe('submitted');
  expect(client.submitPayload()).toEqual({});
  expect(rpc.mock.calls.some(([method]) => method === 'prompt.submit')).toBe(false);
  expect(window.sessionStorage.getItem('hermes.native.attachment-draft:')).not.toMatch(/memory-only|rotated|bytes/);
});
it('creates a new occurrence after removal and preserves attachments outside the accepted set', async () => {
  const { client } = harness(); const file = new File(['bytes'], 'a.txt');
  await client.add([file]); const first = client.getSnapshot().items[0];
  await client.remove(first.occurrence_id); await client.add([file]);
  const second = client.getSnapshot().items[1];
  expect(second.occurrence_id).not.toBe(first.occurrence_id); expect(second.id).not.toBe(first.id);
  client.accepted([first.id!]); expect(client.getSnapshot().items[1].state).toBe('uploaded');
});
it('restores unfinished uploads as requiring file reselection', async () => {
  const { client, ledger } = harness(); await client.add([new File(['bytes'], 'a.txt')]);
  const item = client.getSnapshot().items[0]; ledger.set(item.id!, { ...item, state: 'failed' });
  client.invalidate(true); await client.recover();
  expect(client.getSnapshot().items[0].requiresReselection).toBe(true);
  expect(() => client.submitPayload()).toThrow();
});

it('recovers attachment authority after a lost cancellation ACK without submitting', async () => {
  const { client, rpc } = harness();
  await client.add([new File(['bytes'], 'cancel.txt')]);
  let fail!: (reason: Error) => void;
  rpc.mockImplementationOnce(() => new Promise((_, reject) => { fail = reject; }));
  const removal = client.remove(client.getSnapshot().items[0].occurrence_id);
  fail(new Error('ACK lost')); await removal;
  expect(client.getSnapshot().uncertain).toBe(true);
  await client.recover();
  expect(client.getSnapshot().uncertain).toBe(false);
  expect(client.getSnapshot().items[0].state).toBe('cancelled');
  expect(client.submitPayload().attachment_ids).toBeUndefined();
  expect(rpc.mock.calls.some(([method]) => method === 'prompt.submit')).toBe(false);
});
