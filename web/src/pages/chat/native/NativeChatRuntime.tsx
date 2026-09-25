import { hasInteraction } from "./native-interactions";
import { useMemo, useState, type ReactNode } from "react";
import { AssistantRuntimeProvider, fromThreadMessageLike, type ThreadMessage } from "@assistant-ui/react";
import { useIncrementalExternalStoreRuntime } from "@hermes/chat-ui";
import type { NativeMessage, NativeSessionState } from "./native-types";
import type { NativeSession } from "./native-session";

interface NativeChatRuntimeProps {
  state: NativeSessionState;
  session: NativeSession;
  children: ReactNode;
}

export function NativeChatRuntime({ state, session, children }: NativeChatRuntimeProps) {
  const [cache] = useState(() => new WeakMap<NativeMessage, ThreadMessage>());
  const messageRepository = useMemo(() => {
    const messages = state.conversation.messages.map((source, index) => {
      let message = cache.get(source);
      if (!message) {
        message = fromThreadMessageLike({
          role: source.role,
          metadata: { custom: { sourceId: source.turnId || source.id, partSources: source.parts.map(part => part.sourceId) } },
          content: source.parts.map(part => part.type === "tool" ? {
            type: "tool-call" as const, toolCallId: part.id!, toolName: part.name!, args: { ...part.args, preview: part.text }, argsText: "",
            ...(part.status !== "running" ? { result: { status: part.status, output: part.result, presentation: part.presentation }, isError: part.status === "error" } : {}),
          } : { type: part.type, text: part.text }),
          ...(source.role === "assistant" ? { status: source.pending ? { type: "running" as const } : source.error ? { type: "incomplete" as const, reason: "error" as const, error: source.error } : { type: "complete" as const, reason: "stop" as const } } : {}),
        }, source.id, { type: "complete", reason: "stop" });
        cache.set(source, message);
      }
      return { parentId: state.conversation.messages[index - 1]?.id ?? null, message };
    });
    return { messages, headId: messages.at(-1)?.message.id ?? null };
  }, [state.conversation.messages, cache]);
  const adapter = useMemo(() => ({
    messageRepository,
    isRunning: state.conversation.running,
    isDisabled: !state.ready || hasInteraction(state.interactions),
    onNew: async (message: { content: readonly { type: string; text?: string }[] }) => {
      await session.submit(message.content.filter(part => part.type === "text").map(part => part.text ?? "").join("\n"));
    },
    onCancel: session.interrupt,
  }), [messageRepository, state.conversation.running, state.interactions, state.ready, session]);
  const runtime = useIncrementalExternalStoreRuntime(adapter);
  return <AssistantRuntimeProvider runtime={runtime}>{children}</AssistantRuntimeProvider>;
}
