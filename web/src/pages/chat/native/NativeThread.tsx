import { MessagePrimitive, ThreadPrimitive, type ReasoningMessagePartProps, type TextMessagePartProps, type ToolCallMessagePartProps } from "@assistant-ui/react";

function Text({ text }: TextMessagePartProps) {
  return <div className="whitespace-pre-wrap break-words leading-7">{text}</div>;
}
function Reasoning({ text }: ReasoningMessagePartProps) {
  return <details className="my-3 text-sm opacity-70"><summary className="cursor-pointer">Reasoning</summary><div className="whitespace-pre-wrap py-3">{text}</div></details>;
}
function Tool({ toolName, toolCallId, result, isError, args }: ToolCallMessagePartProps) {
  return <div className="my-3 rounded-lg border border-current/15 px-3 py-2 text-sm" aria-label={`Tool ${toolName}`}>
    <span className="font-medium">{toolName}</span><code className="ml-2 text-xs opacity-50">{toolCallId}</code><span className="ml-3 opacity-65">{isError ? "Error" : result !== undefined ? "Completed" : "Running…"}</span>
    {typeof args.preview === "string" && args.preview ? <div className="mt-1 truncate opacity-60">{args.preview}</div> : null}
  </div>;
}
function UserMessage() {
  return <MessagePrimitive.Root className="ml-auto my-6 max-w-[85%] rounded-2xl bg-current/5 px-5 py-3" aria-label="Your message"><MessagePrimitive.Parts components={{ Text }} /></MessagePrimitive.Root>;
}
function AssistantMessage() {
  return <MessagePrimitive.Root className="my-6" aria-label="Hermes response"><MessagePrimitive.Parts components={{ Text, Reasoning, tools: { Fallback: Tool } }} /></MessagePrimitive.Root>;
}
export function NativeThread() {
  return <ThreadPrimitive.Viewport className="min-h-0 flex-1 overflow-y-auto px-5" autoScroll>
    <div className="mx-auto w-full max-w-3xl pb-6">
      <ThreadPrimitive.Empty><div className="py-20 text-center"><h2 className="text-2xl font-medium">How can Hermes help?</h2><p className="mt-3 text-sm opacity-60">Start a conversation in Native Chat.</p></div></ThreadPrimitive.Empty>
      <ThreadPrimitive.Messages components={{ UserMessage, AssistantMessage }} />
    </div>
  </ThreadPrimitive.Viewport>;
}
