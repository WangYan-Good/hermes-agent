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
}

export interface NativeSessionResponse {
  session_id: string;
  stored_session_id?: string;
  session_key?: string;
  resumed?: string;
  messages?: NativeHistoryMessage[];
  running?: boolean;
  status?: string;
  info?: { stored_session_id?: string; running?: boolean };
  inflight?: { user: string; assistant: string; streaming: boolean; error?: string } | null;
  pending_approval?: unknown;
  pending_clarify?: unknown;
}

export interface NativePart {
  type: "text" | "reasoning" | "tool";
  text: string;
  id?: string;
  name?: string;
  status?: "running" | "complete" | "error";
  sealed?: boolean;
  final?: boolean;
}

export interface NativeMessage {
  id: string;
  role: "user" | "assistant";
  parts: NativePart[];
  error?: string;
  pending?: boolean;
}

export interface NativeConversationState {
  messages: NativeMessage[];
  running: boolean;
  activeId: string | null;
  status: string;
  error: string | null;
  blocked: string | null;
  nextId: number;
}

export interface NativeSessionState {
  runtimeId: string | null;
  storedId: string | null;
  durable: boolean;
  connection: "connecting" | "open" | "error" | "closed";
  ready: boolean;
  conversation: NativeConversationState;
}
