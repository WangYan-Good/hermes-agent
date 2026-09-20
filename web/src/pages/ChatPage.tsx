import ChatSurfaceRouter from "./chat/ChatSurfaceRouter";

export interface ChatPageProps {
  isActive?: boolean;
}

/** Persistent /chat page identity; presentation is selected below this boundary. */
export default function ChatPage(props: ChatPageProps) {
  return <ChatSurfaceRouter {...props} />;
}
