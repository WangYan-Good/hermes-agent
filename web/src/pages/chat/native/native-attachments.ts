import { authedFetch } from '@/lib/api';

export interface AttachmentScope { runtimeId: string; profile: string; generation: number }
export interface NativeAttachment {
  id?: string; occurrence_id: string; request_id: string; name: string; size: number; mime: string;
  state: 'local' | 'uploading' | 'uploaded' | 'submitted' | 'failed' | 'cancelled';
  preview?: string;
  requiresReselection?: boolean;
}
interface AttachmentSnapshot { items: NativeAttachment[]; recovering: boolean; uncertain: boolean; error: string }
interface DraftAuthority { draft_id: string; draft_token: string }
interface DraftLocator { draft_id: string; runtimeId: string; profile: string }
type Request = (method: string, params: Record<string, unknown>) => Promise<unknown>;

function key(profile: string) { return `hermes.native.attachment-draft:${profile}`; }
export function readDraftLocator(profile: string): DraftLocator | null {
  try { const value = JSON.parse(window.sessionStorage.getItem(key(profile)) || 'null'); return value?.profile === profile && typeof value.runtimeId === 'string' && typeof value.draft_id === 'string' ? value : null; } catch { return null; }
}

/** Files, controllers and grants never leave this transient host-side owner. */
export class NativeAttachments {
  private state: AttachmentSnapshot = { items: [], recovering: false, uncertain: false, error: '' };
  private listeners = new Set<() => void>();
  private sources = new Map<string, File>();
  private controllers = new Map<string, AbortController>();
  private authority: DraftAuthority | null = null;
  private preparing: Promise<DraftAuthority> | null = null;
  private epoch = 0;
  private scope: () => AttachmentScope | null;
  private rpc: Request;
  private http: typeof authedFetch;
  constructor(scope: () => AttachmentScope | null, rpc: Request, http = authedFetch) {
    this.scope = scope; this.rpc = rpc; this.http = http;
  }
  getSnapshot = () => this.state;
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener); }; };
  private set(patch: Partial<AttachmentSnapshot>) { this.state = { ...this.state, ...patch }; this.listeners.forEach(f => f()); }
  private current(scope: AttachmentScope, epoch: number) { const now = this.scope(); return epoch === this.epoch && now?.runtimeId === scope.runtimeId && now.profile === scope.profile && now.generation === scope.generation; }
  private update(id: string, patch: Partial<NativeAttachment>) { this.set({ items: this.state.items.map(a => a.occurrence_id === id && a.state !== 'cancelled' ? { ...a, ...patch } : a) }); }
  private params(scope: AttachmentScope) { return { session_id: scope.runtimeId, ...this.authority }; }
  private merge(items: NativeAttachment[]) {
    this.set({ items: items.map(item => {
      const local = this.state.items.find(a => a.occurrence_id === item.occurrence_id);
      if (item.state === 'submitted' || item.state === 'cancelled') {
        if (local?.preview) URL.revokeObjectURL(local.preview);
        this.sources.delete(item.occurrence_id);
        return { ...item, preview: undefined };
      }
      return local?.state === 'cancelled' ? { ...item, ...local, id: item.id } : { ...local, ...item, requiresReselection: !this.sources.has(item.occurrence_id) && ['local', 'uploading', 'failed'].includes(item.state) };
    }) });
  }
  invalidate = (clear = false) => {
    this.epoch++; this.preparing = null; this.authority = null;
    this.controllers.forEach(c => c.abort()); this.controllers.clear();
    if (clear) {
      this.state.items.forEach(a => { if (a.preview) URL.revokeObjectURL(a.preview); });
      this.sources.clear(); this.set({ items: [], error: '', uncertain: false, recovering: false });
    }
  };
  reset = () => { const s = this.scope(); if (s) { try { window.sessionStorage.removeItem(key(s.profile)); } catch { /* storage is optional */ } } this.invalidate(true); };
  recover = async () => {
    const scope = this.scope(); if (!scope) return;
    const locator = readDraftLocator(scope.profile);
    if (!locator || locator.runtimeId !== scope.runtimeId) return;
    const epoch = this.epoch; this.set({ recovering: true, error: '' });
    try {
      const connection = await this.rpc('attachment.connection', { session_id: scope.runtimeId }) as { connection_token: string };
      if (!this.current(scope, epoch)) return;
      const response = await this.http(`/api/chat/attachments/${encodeURIComponent(locator.draft_id)}/recover`, { method: 'POST', headers: { 'X-Hermes-Attachment-Connection': connection.connection_token } });
      if (!response.ok) throw new Error('Draft unavailable');
      const result = await response.json() as DraftAuthority & { attachments: NativeAttachment[] };
      if (!this.current(scope, epoch)) return;
      this.authority = { draft_id: result.draft_id, draft_token: result.draft_token }; this.merge(result.attachments);
      for (const item of this.state.items.filter(a => a.state === 'cancelled' && a.id)) {
        await this.rpc('attachment.cancel', { ...this.params(scope), attachment_id: item.id });
        if (!this.current(scope, epoch)) return;
      }
      // A submitted ledger record is an accepted turn, never a retry candidate.
      this.set({ uncertain: false });
    } catch { if (this.current(scope, epoch)) this.set({ error: 'Could not recover attachments. Reconnect to inspect the backend draft.', uncertain: true }); }
    finally { if (this.current(scope, epoch)) this.set({ recovering: false }); }
  };
  private async ensure(scope: AttachmentScope, epoch: number): Promise<DraftAuthority> {
    if (this.authority) return this.authority;
    if (this.preparing) return this.preparing;
    this.preparing = (async () => {
      const result = await this.rpc('attachment.prepare', { session_id: scope.runtimeId }) as DraftAuthority;
      if (!this.current(scope, epoch)) throw new Error('Stale draft');
      const connection = await this.rpc('attachment.connection', { session_id: scope.runtimeId }) as { connection_token: string };
      const response = await this.http(`/api/chat/attachments/${encodeURIComponent(result.draft_id)}/recover`, { method: 'POST', headers: { 'X-Hermes-Attachment-Connection': connection.connection_token, 'X-Hermes-Attachment-Token': result.draft_token } });
      if (!response.ok || !this.current(scope, epoch)) throw new Error('Draft recovery unavailable');
      this.authority = result;
      try { window.sessionStorage.setItem(key(scope.profile), JSON.stringify({ draft_id: result.draft_id, runtimeId: scope.runtimeId, profile: scope.profile })); } catch { /* no binary or authorization in storage */ }
      return result;
    })();
    try { return await this.preparing; } finally { if (epoch === this.epoch) this.preparing = null; }
  }
  add = async (files: File[]) => {
    const scope = this.scope(); const epoch = this.epoch;
    if (!scope) return;
    const batch = new Set<string>();
    for (const file of files) {
      if (!this.current(scope, epoch)) return;
      const signature = `${file.name}:${file.size}:${file.lastModified}`;
      if (batch.has(signature)) continue; batch.add(signature);
      const active = this.state.items.filter(a => !['submitted', 'cancelled'].includes(a.state));
      const limit = file.type.startsWith('image/') && file.type !== 'image/svg+xml' ? 25 : 100;
      if (!file.size || file.size > limit * 1024 * 1024 || active.length >= 10 || active.reduce((sum, a) => sum + a.size, 0) + file.size > 200 * 1024 * 1024 || ([...file.name].some(char => char.charCodeAt(0) < 32) || /[\\/]/.test(file.name))) {
        this.set({ error: 'Attachment rejected: check filename, nonempty content, size and count limits.' }); continue;
      }
      const id = crypto.randomUUID();
      this.sources.set(id, file);
      const preview = /^image\/(png|jpeg|gif|webp|bmp)$/.test(file.type) && typeof URL.createObjectURL === 'function' ? URL.createObjectURL(file) : undefined;
      this.set({ items: [...this.state.items, { occurrence_id: id, request_id: crypto.randomUUID(), name: file.name, size: file.size, mime: file.type || 'application/octet-stream', state: 'local', preview }], error: '' });
      // Sequential upload stays below the server's concurrency cap and bounds memory.
      await this.retry(id);
    }
  };
  retry = async (occurrence: string) => {
    const scope = this.scope(); const source = this.sources.get(occurrence);
    const item = this.state.items.find(a => a.occurrence_id === occurrence);
    if (!scope || !source || !item || !['local', 'failed'].includes(item.state) || this.state.uncertain) return;
    const epoch = this.epoch; const controller = new AbortController(); this.controllers.set(occurrence, controller);
    this.update(occurrence, { state: 'uploading' });
    try {
      await this.ensure(scope, epoch);
      if (!this.current(scope, epoch) || controller.signal.aborted) return;
      const prepared = await this.rpc('attachment.prepare', { ...this.params(scope), occurrence_id: occurrence, upload_request_id: item.request_id, name: item.name, size: item.size, mime: item.mime }) as { attachment: NativeAttachment; upload_token: string };
      if (!this.current(scope, epoch)) return;
      if (controller.signal.aborted) { await this.rpc('attachment.cancel', { ...this.params(scope), attachment_id: prepared.attachment.id }); return; }
      this.update(occurrence, { id: prepared.attachment.id });
      const authority = this.authority!;
      const response = await this.http(`/api/chat/attachments/${encodeURIComponent(authority.draft_id)}/${encodeURIComponent(prepared.attachment.id!)}`, { method: 'PUT', body: source, signal: controller.signal, headers: { 'X-Hermes-Attachment-Token': authority.draft_token, 'X-Hermes-Runtime': scope.runtimeId, 'X-Hermes-Attachment-Upload': prepared.upload_token } });
      if (!response.ok) throw new Error('Upload rejected');
      const completed = await response.json() as NativeAttachment;
      if (this.current(scope, epoch) && !controller.signal.aborted && completed.id === prepared.attachment.id && completed.occurrence_id === occurrence) this.update(occurrence, completed);
    } catch {
      if (this.current(scope, epoch) && !controller.signal.aborted) {
        try {
          const snapshot = await this.rpc('attachment.snapshot', this.params(scope)) as { attachments: NativeAttachment[] };
          if (this.current(scope, epoch)) {
            const owned = snapshot.attachments.find(a => a.occurrence_id === occurrence);
            this.update(occurrence, owned?.state === 'uploaded' ? owned : { state: 'failed' });
          }
        } catch { if (this.current(scope, epoch)) this.update(occurrence, { state: 'failed' }); }
        if (this.current(scope, epoch)) this.set({ error: 'Upload was not confirmed. Inspect the attachment before retrying.' });
      }
    } finally { if (this.controllers.get(occurrence) === controller) this.controllers.delete(occurrence); }
  };
  remove = async (occurrence: string) => {
    if (this.state.uncertain) return;
    const item = this.state.items.find(a => a.occurrence_id === occurrence); if (!item || item.state === 'submitted') return;
    const scope = this.scope(); this.controllers.get(occurrence)?.abort();
    this.update(occurrence, { state: 'cancelled' }); this.sources.delete(occurrence);
    if (item.preview) URL.revokeObjectURL(item.preview);
    if (item.id && scope && this.authority) {
      try { await this.rpc('attachment.cancel', { ...this.params(scope), attachment_id: item.id }); }
      catch { this.set({ error: 'Removal was not confirmed by the server. Reconnect to inspect the draft.' }); }
    }
  };
  submitPayload(): Partial<DraftAuthority> & { attachment_ids?: string[] } {
    const selected = this.state.items.filter(a => !['cancelled', 'submitted'].includes(a.state));
    if (!selected.length) return {};
    if (!this.authority || selected.some(a => a.state !== 'uploaded') || this.state.recovering || this.state.uncertain) throw new Error('Attachments are not ready');
    return { ...this.authority, attachment_ids: selected.map(a => a.id!) };
  }
  markUncertain() { if (this.state.items.some(a => a.state === 'uploaded')) this.set({ uncertain: true }); }
  accepted(ids: string[]) {
    this.set({ items: this.state.items.map(a => a.state === 'uploaded' && a.id && ids.includes(a.id) ? { ...a, state: 'submitted' } : a), uncertain: false });
    this.state.items.forEach(a => { if (a.state === 'submitted') { if (a.preview) URL.revokeObjectURL(a.preview); this.sources.delete(a.occurrence_id); } });
  }
}
