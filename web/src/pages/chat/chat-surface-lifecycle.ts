export interface SurfaceStatus {
  ready: boolean;
  blocked: string[];
  storedId?: string | null;
  released?: boolean;
}
export interface ChatSurfaceLifecycle {
  status(): Promise<SurfaceStatus>;
  subscribe(listener: () => void): () => void;
  prepare(): Promise<SurfaceStatus>;
  cancel(): Promise<void>;
  release(): Promise<void>;
  discard(): Promise<void>;
  dispose(): Promise<void>;
  /** Permanent host departure; revoke local input without claiming backend release. */
  detach(): void;
  setInput(enabled: boolean): void;
}
