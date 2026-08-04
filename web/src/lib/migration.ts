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
): { stage: MigrationStage; status: string; detail: string } | null {
  const m = /^\[([a-z_]+)\]\s+(\S+)\s*(.*)$/.exec(line.trim());
  if (!m || !STAGE_SET.has(m[1])) return null;
  return { stage: m[1] as MigrationStage, status: m[2], detail: m[3] ?? "" };
}

const ID_RE = /^[a-z0-9][a-z0-9_-]*$/;

/** Client-side mirror of validate_target. The server still validates; this
 *  exists so the form can refuse before a round trip. */
export function validateTargetDraft(d: Record<string, string>): string | null {
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
