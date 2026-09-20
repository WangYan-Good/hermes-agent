import type { ChatPageProps } from "../ChatPage";
import type { ChatMode } from "./chat-mode";
import TerminalChatPage from "./TerminalChatPage";
import { useChatMode } from "./use-chat-mode";

const surfaces: Partial<Record<ChatMode, typeof TerminalChatPage>> = {
  terminal: TerminalChatPage,
};

/** No transport/session ownership here, and no mode-based React key. */
export default function ChatSurfaceRouter(props: ChatPageProps) {
  const { effective } = useChatMode();
  const Surface = surfaces[effective] ?? TerminalChatPage;
  return <Surface {...props} />;
}
