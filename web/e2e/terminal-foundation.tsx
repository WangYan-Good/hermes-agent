/** Vite-only test entry. Not a production route or production build input. */
import { createRoot } from 'react-dom/client';
import TerminalChatPage from '../src/pages/chat/TerminalChatPage';
import type { TerminalLifecycle } from '../src/pages/chat/terminal-lifecycle';
const root = createRoot(document.getElementById('root')!);
const scope = window as unknown as { terminal: TerminalLifecycle | null; unmount: () => void };
const params = new URLSearchParams(location.search);
root.render(<TerminalChatPage profile={params.get('profile') ?? ''} resume={params.get('resume') ?? undefined} onLifecycle={value => { scope.terminal = value; }} />);
scope.unmount = () => root.unmount();
