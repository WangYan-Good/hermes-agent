// @vitest-environment jsdom
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import TerminalChatPage from './TerminalChatPage';
const harness = vi.hoisted(() => ({ term: null as null | { data: (text: string) => void; textarea: HTMLTextAreaElement; write: ReturnType<typeof vi.fn>; dispose: ReturnType<typeof vi.fn>; key: (event: object) => boolean }, transport: null as null | { input: ReturnType<typeof vi.fn>; resize: ReturnType<typeof vi.fn>; dispose: ReturnType<typeof vi.fn>; output: (data: Uint8Array) => void } }));
vi.mock('@xterm/xterm', () => ({ Terminal: class {
  textarea = document.createElement('textarea'); cols = 80; rows = 24; unicode = { activeVersion: '' };
  buffer = { active: { viewportY: 0, baseY: 0 } }; write = vi.fn(); dispose = vi.fn();
  data: (text: string) => void = () => {}; key: (event: object) => boolean = () => true;
  constructor() { harness.term = this; }
  loadAddon() {} open(node: HTMLElement) { node.append(this.textarea); } scrollToBottom() {}
  onData(callback: (text: string) => void) { this.data = callback; return { dispose() {} }; }
  onBinary() { return { dispose() {} }; }
  hasSelection() { return false; } getSelection() { return ''; }
  attachCustomKeyEventHandler(callback: (event: object) => boolean) { this.key = callback; }
} }));
vi.mock('@xterm/addon-fit', () => ({ FitAddon: class { fit() {} } }));
vi.mock('@xterm/addon-unicode11', () => ({ Unicode11Addon: class {} }));
vi.mock('@xterm/addon-web-links', () => ({ WebLinksAddon: class {} }));
vi.mock('./terminal-lifecycle', () => ({ TerminalLifecycle: class {
  input = vi.fn(); resize = vi.fn(); dispose = vi.fn(); connect = vi.fn(); output: (bytes: Uint8Array) => void;
  constructor(options: { output: (bytes: Uint8Array) => void }) { this.output = options.output; harness.transport = this; }
} }));
let node: HTMLDivElement;
let root: ReturnType<typeof createRoot>;
beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
  node = document.createElement('div'); document.body.append(node); root = createRoot(node);
});
afterEach(async () => { await act(async () => root.unmount()); node.remove(); vi.unstubAllGlobals(); });
it('mounts xterm, renders raw bytes, forwards keyboard/wheel but not mouse motion, and cleans up', async () => {
  await act(async () => root.render(<TerminalChatPage profile="work" />));
  const term = harness.term!, transport = harness.transport!;
  transport.output(new Uint8Array([65])); expect(term.write).toHaveBeenCalled();
  term.data('hello'); term.data('\x1b[<64;1;2M'); term.data('\x1b[<32;1;2M');
  expect(transport.input.mock.calls.map(call => call[0])).toEqual(['hello', '\x1b[<64;1;2M']);
  term.key({ type: 'keydown', ctrlKey: true, key: 'Backspace' });
  expect(transport.input).toHaveBeenLastCalledWith('\x17');
  await act(async () => root.render(<TerminalChatPage profile="other" />));
  expect(transport.dispose).toHaveBeenCalledTimes(1); expect(term.dispose).toHaveBeenCalledTimes(1);
});
it('forwards an IME/mobile commit exactly once when xterm emits it', async () => {
  await act(async () => root.render(<TerminalChatPage profile="" />));
  const term = harness.term!, transport = harness.transport!;
  term.textarea.dispatchEvent(new CompositionEvent('compositionend', { data: '中文' }));
  term.data('中文');
  await new Promise(resolve => setTimeout(resolve, 25));
  expect(transport.input).toHaveBeenCalledExactlyOnceWith('中文');
});
