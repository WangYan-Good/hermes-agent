import type { ChatTranscriptPart } from '@hermes/chat-ui';
import type { Interactions } from "./native-interactions";
import type { NativeControl } from "./native-control";
/** Wire contracts audited against tui_gateway's session and prompt handlers. */
export interface NativeHistoryMessage {
  role: "user" | "assistant" | "tool" | "system";
  text?: string;
  name?: string;
  context?: string;
  row_id?: number;
  reasoning?: string;
  reasoning_content?: string;
  reasoning_details?: unknown;
  codex_reasoning_items?: unknown;
  display_kind?: string;
  display_metadata?: Record<string, unknown>;
  tool_call_id?: string;
  tool_calls?: unknown[];
  result?: unknown;
}

export interface NativeSessionResponse {
  session_id: string;
  stored_session_id?: string;
  session_key?: string;
  resumed?: string;
  messages?: NativeHistoryMessage[];
  running?: boolean;
  status?: string;
  info?: { stored_session_id?: string; running?: boolean; usage?: Record<string, unknown> };
  inflight?: { turn_id?: string; user: string; assistant: string; streaming: boolean; error?: string } | null;
  pending_approval?: unknown;
  pending_clarify?: unknown;
  pending_interactions?: { type: string; payload: unknown }[];
  queued?: { user?: string };
  durable_rows?: Record<string, unknown>[];
}

export type NativePart = ChatTranscriptPart;

export interface NativeMessage {
  id: string;
  role: "user" | "assistant";
  parts: NativePart[];
  error?: string;
  pending?: boolean;
  turnId?: string;
}

export interface NativeConversationState {
  messages: NativeMessage[];
  running: boolean;
  activeId: string | null;
  status: string;
  error: string | null;
  nextId: number;
}

export interface NativeSessionState {
  runtimeId: string | null;
  storedId: string | null;
  durable: boolean;
  connection: "connecting" | "open" | "error" | "closed";
  ready: boolean;
  conversation: NativeConversationState;
  interactions: Interactions;
  control: NativeControl;
}
