import ChatSurfaceRouter from "./chat/ChatSurfaceRouter";

export interface ChatPageProps {
  isActive?: boolean;
}

/** App keeps this surface router mounted when another Dashboard route is visible. */
export default function ChatPage(props: ChatPageProps) {
  return <ChatSurfaceRouter {...props} />;
}
