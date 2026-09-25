import { describe, expect, it } from 'vitest';
import { emptyConversation, reduceNativeEvent } from './native-events';
import * as history from './native-messages';

describe('rich native projection', () => {
  it('preserves structured complete-before-start results and ignores late starts', () => {
    const event = { type: 'tool.complete', session_id: 'r', payload: { tool_id: 'call-1', name: 'write_file', args: { path: 'app.py' }, result: { success: true }, presentation: { version: 1, tool_call_id: 'call-1', changes: [{ path: 'app.py', diff: '+hello' }] } } };
    const state = reduceNativeEvent(emptyConversation(), event);
    expect(state.messages[0].parts[0].result).toEqual({ success: true });
    expect(state.messages[0].parts[0].args).toEqual({ path: 'app.py' });
    expect(state.messages[0].parts[0].presentation?.changes?.[0].path).toBe('app.py');
    const twice = reduceNativeEvent(state, event);
    expect(twice.messages[0].parts).toHaveLength(1);
    expect(reduceNativeEvent(twice, { ...event, type: 'tool.start' }).messages[0].parts[0].status).toBe('complete');
  });
  it('joins durable calls/results by tool id and retains file references', () => {
    expect(typeof history.hydrateDurableHistory).toBe('function');
    const state = history.hydrateDurableHistory([
      { id: 1, role: 'user', content: 'Read @file:report.txt', display_metadata: { turn_id: 'turn-1' } },
      { id: 2, role: 'assistant', content: '', tool_calls: [{ id: 'call-1', function: { name: 'read_file', arguments: '{"path":"report.txt"}' } }] },
      { id: 3, role: 'tool', tool_call_id: 'call-1', tool_name: 'read_file', content: '{"text":"full result"}' },
      { id: 4, role: 'assistant', content: 'Done' },
    ]);
    expect(state.messages[0].parts[0].text).toContain('@file:report.txt');
    const tools = state.messages.flatMap(m => m.parts).filter(p => p.type === 'tool');
    expect(tools).toHaveLength(1);
    expect(tools[0].id).toBe('call-1');
    expect(tools[0].result).toEqual({ text: 'full result' });
  });
});

it('keeps the persisted content source through completion and partial-page hydration', () => {
  const source = 'stored:row:42:content:0';
  const live = reduceNativeEvent(emptyConversation(), { type: 'message.complete', session_id: 'r', payload: { text: 'artifact', turn_id: 'turn', content_sources: [source] } });
  const durable = history.hydrateDurableHistory([{ id: 42, role: 'assistant', content: 'artifact', display_metadata: { turn_id: 'turn', content_source: source } }]);
  expect(live.messages[0].parts[0].sourceId).toBe(source);
  expect(durable.messages[0].parts[0].sourceId).toBe(source);
  expect(durable.messages[0].turnId).toBe('turn');
});
