/** Pure mappings for the migration UI.
 *
 *  Lives in lib/ because this repo's frontend tests cover lib/ only — logic
 *  inside a component cannot be tested here at all. */

export type PreflightTier = "blocking" | "warning";

export interface PreflightCheck {
  name: string;
  tier: PreflightTier;
  ok: boolean;
  detail: string;
}

export type MigrationStage =
  | "install"
  | "stop_source"
  | "backup"
  | "transfer"
  | "restore"
  | "verify";

/** A notice is not a progress stage, but part of the completion cleanup.
 *  It must be distinguishable from stage events by checking `stage === "cleanup"`. */
export type MigrationStageOrNotice = MigrationStage | "cleanup";

/** Same order as migration_admin.STAGES; install precedes stop_source. */
export const MIGRATION_STAGES: readonly MigrationStage[] = [
  "install",
  "stop_source",
  "backup",
  "transfer",
  "restore",
  "verify",
] as const;

const STAGE_SET = new Set<string>(MIGRATION_STAGES);

/** A failed warning is amber, never red: it does not stop the migration. */
export function checkTone(c: PreflightCheck): "ok" | "warn" | "error" {
  if (c.ok) return "ok";
  return c.tier === "blocking" ? "error" : "warn";
}

export function isBlocked(checks: PreflightCheck[]): boolean {
  return checks.some((c) => c.tier === "blocking" && !c.ok);
}

/** Whether the confirm-overwrite checkbox should be offered at all: true
 *  exactly when the *only* blocking failures are `target_home` (existing
 *  state on the target). A failed `python3` or `disk_space` check, say,
 *  cannot be waived by consenting to overwrite unrelated data. */
export function needsOverwriteConfirm(checks: PreflightCheck[]): boolean {
  const blocking = checks.filter((c) => c.tier === "blocking" && !c.ok);
  return blocking.length > 0 && blocking.every((c) => c.name === "target_home");
}

/** Whether the Start button should be enabled. Mirrors `isBlocked`, but a
 *  target_home-only block can be overridden by an explicit, unchecked-by-
 *  default confirmation — every other blocking failure cannot. */
export function canStartMigration(
  checks: PreflightCheck[],
  confirmOverwrite: boolean,
): boolean {
  if (!isBlocked(checks)) return true;
  return needsOverwriteConfirm(checks) && confirmOverwrite;
}

export function stageProgress(
  stage: MigrationStage | null,
  status: "start" | "ok" | "fail",
): number {
  if (!stage) return 0;
  const idx = MIGRATION_STAGES.indexOf(stage);
  if (idx < 0) return 0;
  const total = MIGRATION_STAGES.length;
  // A started-or-failed stage counts as reaching its own boundary; only "ok"
  // credits its completion, so a failure never appears to advance.
  const completed = status === "ok" ? idx + 1 : idx;
  return Math.round((completed / total) * 100);
}

export function parseActionLine(
  line: string,
): { stage: MigrationStageOrNotice; status: string; detail: string } | null {
  const m = /^\[([a-z_]+)\]\s+(\S+)\s*(.*)$/.exec(line.trim());
  if (!m) return null;
  const stageName = m[1];
  if (!STAGE_SET.has(stageName) && stageName !== "cleanup") return null;
  return { stage: stageName as MigrationStageOrNotice, status: m[2], detail: m[3] ?? "" };
}

/** True when an error message is ssh's own host-key-mismatch failure
 *  (`_SSH_FAILURE_SIGNATURES` in `hermes_cli/remote_exec.py` always includes
 *  the literal substring "host key verification failed" on that path) —
 *  as opposed to any other transport or remote-command failure. Callers use
 *  this to swap the raw stderr for `t.migration.fingerprintChanged`. */
export function isHostKeyMismatchError(message: string): boolean {
  return /host key/i.test(message);
}

interface StartCommandTarget {
  host: string;
  user: string;
  port: number;
  identity_file: string;
  target_home: string;
}

/** The command an operator runs, from their own machine, to bring the
 *  migrated instance up on the target. Migration deliberately stops at a
 *  verified-but-idle target — promotion is a human decision — so this is
 *  display copy, never invoked automatically. */
export function migrationStartCommand(target: StartCommandTarget): string {
  const home = target.target_home.trim() || "~/.hermes";
  const argv = ["ssh"];
  if (target.port && target.port !== 22) argv.push("-p", String(target.port));
  if (target.identity_file.trim()) argv.push("-i", target.identity_file.trim());
  argv.push(`${target.user}@${target.host}`);
  argv.push(`"HERMES_HOME=${home} hermes gateway start"`);
  return argv.join(" ");
}

const ID_RE = /^[a-z0-9][a-z0-9_-]*$/;

/** Client-side mirror of validate_target. The server still validates; this
 *  exists so the form can refuse before a round trip. */
export function validateTargetDraft(d: Record<string, string>): string | null {
  if ("password" in d) {
    return "password authentication is not supported: use an SSH key instead";
  }
  if (!ID_RE.test(d.id ?? "")) {
    return "id must be lowercase alphanumeric with - or _";
  }
  if (!(d.host ?? "").trim()) return "host is required";
  if (!(d.user ?? "").trim()) return "user is required";
  if (d.port) {
    const port = Number(d.port);
    if (!Number.isInteger(port) || port < 1 || port > 65535) {
      return "port must be between 1 and 65535";
    }
  }
  return null;
}

/** Shape a target-editor draft into the create/update request body. Blank
 *  optional fields are omitted rather than sent as `""` — a blank port in
 *  particular must not reach the API as an empty string, which cannot
 *  coerce to int server-side and would 422 instead of falling back to the
 *  default port 22. */
export function toTargetPayload(draft: Record<string, string>): Record<string, unknown> {
  const payload: Record<string, unknown> = {
    id: (draft.id ?? "").trim(),
    host: (draft.host ?? "").trim(),
    user: (draft.user ?? "").trim(),
  };
  const label = (draft.label ?? "").trim();
  if (label) payload.label = label;
  const identityFile = (draft.identity_file ?? "").trim();
  if (identityFile) payload.identity_file = identityFile;
  const targetHome = (draft.target_home ?? "").trim();
  if (targetHome) payload.target_home = targetHome;
  const port = (draft.port ?? "").trim();
  if (port) payload.port = Number(port);
  return payload;
}
