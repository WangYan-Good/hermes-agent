import { useEffect, useRef, useState } from 'react';
import { Terminal } from '@xterm/xterm';
import { FitAddon } from '@xterm/addon-fit';
import { Unicode11Addon } from '@xterm/addon-unicode11';
import { WebLinksAddon } from '@xterm/addon-web-links';
import '@xterm/xterm/css/xterm.css';
import { copyTextToClipboard } from '@/lib/clipboard';
import { createPtyCompositionForwarder } from '@/lib/pty-composition';
import { TerminalLifecycle } from './terminal-lifecycle';

export interface TerminalChatPageProps {
  profile: string;
  resume?: string;
  onLifecycle?: (lifecycle: TerminalLifecycle | null) => void;
}

/** Infrastructure component; deliberately absent from the production /chat route. */
export default function TerminalChatPage({ profile, resume, onLifecycle }: TerminalChatPageProps) {
  const host = useRef<HTMLDivElement>(null);
  const [state, setState] = useState('connecting');
  useEffect(() => {
    if (!host.current) return;
    const term = new Terminal({ convertEol: false, cursorBlink: true, scrollback: 5000, allowProposedApi: true });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.loadAddon(new Unicode11Addon());
    term.unicode.activeVersion = '11';
    term.loadAddon(new WebLinksAddon());
    term.open(host.current);
    let disposed = false;
    const lifecycle = new TerminalLifecycle({
      profile, resume,
      state: next => { if (!disposed) { setState(next); if (next === 'ready') lifecycle.resize(term.cols, term.rows); } },
      output: bytes => {
        if (disposed) return;
        const follow = term.buffer.active.viewportY >= term.buffer.active.baseY;
        // Raw UTF-8/ANSI stays with xterm's streaming parser. Reconnect repaint
        // is owned by the PTY, not a lossy escape-sequence sanitizer.
        term.write(bytes, () => { if (!disposed && follow) term.scrollToBottom(); });
      },
    });
    const send = (data: string) => {
      // Ink owns wheel scrolling; suppress selection/motion mouse reports.
      // eslint-disable-next-line no-control-regex
      const mouse = /^\x1b\[<(\d+);\d+;\d+([Mm])$/.exec(data);
      if (mouse && (mouse[2] !== 'M' || (Number(mouse[1]) & 64) === 0 || (Number(mouse[1]) & 32) !== 0)) return;
      lifecycle.input(data);
    };
    const composition = createPtyCompositionForwarder(send);
    const data = term.onData(text => { composition.noteTerminalData(text); send(text); });
    const binary = term.onBinary(text => send(text));
    const composed = (event: CompositionEvent) => composition.onCompositionEnd(event.data);
    term.textarea?.addEventListener('compositionend', composed);
    term.attachCustomKeyEventHandler(event => {
      if (event.type !== 'keydown') return true;
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'c' && term.hasSelection()) {
        void copyTextToClipboard(term.getSelection());
        return false;
      }
      if (event.ctrlKey && !event.altKey && !event.metaKey && ['Backspace', 'Delete'].includes(event.key)) {
        send(event.key === 'Backspace' ? '\x17' : '\x1bd');
        return false;
      }
      return true;
    });
    const resize = () => { if (!disposed) { fit.fit(); lifecycle.resize(term.cols, term.rows); } };
    const observer = new ResizeObserver(resize);
    observer.observe(host.current);
    resize();
    onLifecycle?.(lifecycle);
    void lifecycle.connect();
    return () => {
      disposed = true;
      observer.disconnect();
      term.textarea?.removeEventListener('compositionend', composed);
      composition.dispose(); data.dispose(); binary.dispose();
      onLifecycle?.(null);
      void lifecycle.dispose();
      term.dispose();
    };
  }, [profile, resume, onLifecycle]);
  return <section aria-label="Terminal Chat" className="flex h-full min-h-0 flex-col">
    <output role="status">{state}</output>
    <div ref={host} className="min-h-0 flex-1" />
  </section>;
}
