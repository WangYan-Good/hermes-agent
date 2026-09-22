import { JsonRpcGatewayClient, type GatewayClientOptions } from "@hermes/shared";
import { buildWsUrl } from "@/lib/api";
import { clearDashboardTokenReloadAttempt, maybeReloadForLoopbackWsAuthFailure } from "@/lib/dashboard-auth-reload";

export function bounded<T>(promise: Promise<T>, ms: number, message: string): Promise<T> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(message)), ms);
    promise.then(value => { clearTimeout(timer); resolve(value); }, error => { clearTimeout(timer); reject(error); });
  });
}

/** Web authentication/lifecycle only; framing and pending RPCs stay shared. */
export class NativeGateway extends JsonRpcGatewayClient {
  authFailed = false;
  private cancelled = false;

  constructor(options: GatewayClientOptions = {}) {
    super({ ...options, requestTimeoutMs: 60_000, onSocketClose: event => {
      if (event.code === 4401 || event.code === 4403) this.authFailed = true;
      maybeReloadForLoopbackWsAuthFailure(event.code);
    } });
  }

  async open(): Promise<void> {
    const url = await bounded(buildWsUrl("/api/ws"), 15_000, "Authentication timed out. Retry the connection.");
    if (this.cancelled) return;
    await super.connect(url);
    if (!this.cancelled) clearDashboardTokenReloadAttempt();
  }

  override close(): void {
    this.cancelled = true;
    super.close();
  }
}
