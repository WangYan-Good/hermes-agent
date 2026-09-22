import { lazy, Suspense, type ComponentType } from "react";
import { useLocation } from "react-router";
import type { ChatPageProps } from "../ChatPage";
import type { ChatMode } from "./chat-mode";
import TerminalChatPage from "./TerminalChatPage";
import { useChatMode } from "./use-chat-mode";

const surfaces: Partial<Record<ChatMode, ComponentType<ChatPageProps>>> = {
  terminal: TerminalChatPage,
  native: lazy(() => import("./native/NativeChatPage")),
};

/** No transport/session ownership here, and no mode-based React key. */
export default function ChatSurfaceRouter(props: ChatPageProps) {
  const { search } = useLocation();
  const { effective } = useChatMode(search);
  const Surface = surfaces[effective] ?? TerminalChatPage;
  return <Suspense fallback={<div role="status">Loading chat…</div>}><Surface {...props} /></Suspense>;
}
