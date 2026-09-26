import type { ChatSurfaceLifecycle } from "./chat/chat-switch";
import ChatSurfaceRouter from "./chat/ChatSurfaceRouter";

export interface ChatPageProps {
  isActive?: boolean;
  inputEnabled?: boolean;
  handoffResume?: string | null;
  registerLifecycle?: (adapter: ChatSurfaceLifecycle) => () => void;
}

/** Persistent /chat page identity; presentation is selected below this boundary. */
export default function ChatPage(props: ChatPageProps) {
  return <ChatSurfaceRouter {...props} />;
}
