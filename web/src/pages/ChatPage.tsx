import NativeChatPage from "./chat/native/NativeChatPage";

export interface ChatPageProps {
  isActive?: boolean;
}

/** App keeps this Native host mounted when another Dashboard route is visible. */
export default function ChatPage(props: ChatPageProps) {
  return <NativeChatPage {...props} />;
}
