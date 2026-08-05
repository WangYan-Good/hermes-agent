import { useCallback, useEffect, useState } from "react";
import { Archive, Plus, Server, Trash2, TriangleAlert } from "lucide-react";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@nous-research/ui/ui/components/card";
import { Checkbox } from "@nous-research/ui/ui/components/checkbox";
import { CommandBlock } from "@nous-research/ui/ui/components/command-block";
import { Input } from "@nous-research/ui/ui/components/input";
import { Label } from "@nous-research/ui/ui/components/label";
import { Spinner } from "@nous-research/ui/ui/components/spinner";

import { ActionLogViewer } from "@/components/ActionLogViewer";
import { api, type MigrationTarget } from "@/lib/api";
import {
  canStartMigration,
  checkTone,
  isHostKeyMismatchError,
  migrationStartCommand,
  needsOverwriteConfirm,
  parseActionLine,
  stageProgress,
  toTargetPayload,
  validateTargetDraft,
  type MigrationStage,
  type PreflightCheck,
} from "@/lib/migration";
import { useI18n } from "@/i18n";

const MIGRATE_ACTION = "migrate-host";

type Draft = Record<string, string>;

const EMPTY_DRAFT: Draft = {
  id: "",
  label: "",
  host: "",
  user: "",
  port: "",
  identity_file: "",
  target_home: "",
};

/**
 * Backup/migration hub. Two sections: local backup & restore (moved here from
 * SystemPage in a follow-up task) and migrating the whole instance to another
 * host.
 *
 * This component deliberately holds no product rules — every decision comes
 * from `@/lib/migration`, because this repo's frontend tests cover `lib/` only
 * and there is no component-test setup, so logic placed here is untestable.
 */
export default function MigratePage() {
  const { t } = useI18n();
  const copy = t.migration;

  const [targets, setTargets] = useState<MigrationTarget[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [formError, setFormError] = useState<string | null>(null);

  const [checks, setChecks] = useState<PreflightCheck[]>([]);
  const [preflighting, setPreflighting] = useState(false);
  const [preflightError, setPreflightError] = useState<string | null>(null);

  const [confirmOverwrite, setConfirmOverwrite] = useState(false);
  const [running, setRunning] = useState(false);
  const [stage, setStage] = useState<MigrationStage | null>(null);
  const [stageStatus, setStageStatus] = useState<"start" | "ok" | "fail">("start");
  const [cleanupWarning, setCleanupWarning] = useState<string | null>(null);
  const [finished, setFinished] = useState(false);

  const refresh = useCallback(async () => {
    try {
      setTargets((await api.listMigrationTargets()).targets);
    } catch (e) {
      setFormError(String(e));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const target = targets.find((x) => x.id === selected) ?? null;

  const resetRunState = () => {
    setChecks([]);
    setPreflightError(null);
    setConfirmOverwrite(false);
    setRunning(false);
    setStage(null);
    setStageStatus("start");
    setCleanupWarning(null);
    setFinished(false);
  };

  const handleSave = async () => {
    if (!draft) return;
    const invalid = validateTargetDraft(draft);
    if (invalid) {
      setFormError(invalid);
      return;
    }
    setFormError(null);
    try {
      const body = toTargetPayload(draft);
      const exists = targets.some((x) => x.id === draft.id);
      if (exists) await api.updateMigrationTarget(draft.id, body);
      else await api.createMigrationTarget(body);
      setDraft(null);
      await refresh();
    } catch (e) {
      setFormError(String(e));
    }
  };

  const handleDelete = async (id: string) => {
    try {
      await api.deleteMigrationTarget(id);
      if (selected === id) {
        setSelected(null);
        resetRunState();
      }
      await refresh();
    } catch (e) {
      setFormError(String(e));
    }
  };

  const handlePreflight = async () => {
    if (!target) return;
    setPreflighting(true);
    setPreflightError(null);
    setChecks([]);
    try {
      setChecks((await api.preflightMigrationTarget(target.id)).checks);
    } catch (e) {
      const message = String(e);
      // A changed host key is a decision for a human, not a raw stderr dump:
      // this channel carries plaintext .env and auth.json.
      setPreflightError(
        isHostKeyMismatchError(message) ? copy.fingerprintChanged : message,
      );
    } finally {
      setPreflighting(false);
    }
  };

  const handleStart = async () => {
    if (!target) return;
    setCleanupWarning(null);
    setFinished(false);
    try {
      await api.startMigration(target.id, confirmOverwrite);
      setRunning(true);
    } catch (e) {
      setPreflightError(String(e));
    }
  };

  // The log tail is the progress source: ActionLogViewer already polls the
  // action status, so we piggyback on it rather than opening a second poll.
  const handleLines = useCallback((lines: string[]) => {
    for (const line of lines) {
      const parsed = parseActionLine(line);
      if (!parsed) continue;
      if (parsed.stage === "cleanup") {
        // The only signal that a plaintext archive was left on the target.
        setCleanupWarning(parsed.detail);
        continue;
      }
      setStage(parsed.stage);
      setStageStatus(parsed.status === "ok" || parsed.status === "fail"
        ? parsed.status
        : "start");
    }
  }, []);

  const percent = stageProgress(stage, stageStatus);
  const blockedByOverwrite = needsOverwriteConfirm(checks);
  const canStart = checks.length > 0 && canStartMigration(checks, confirmOverwrite);

  return (
    <div className="flex flex-col gap-6">
      <Card>
        <CardHeader className="border-b border-border bg-card">
          <div className="flex items-center gap-2">
            <Archive className="h-5 w-5 text-muted-foreground" />
            <CardTitle className="text-base">Backup &amp; restore</CardTitle>
          </div>
          <CardDescription>
            Create a full backup of this instance, or restore from a previously
            created one.
          </CardDescription>
        </CardHeader>
        <CardContent className="py-8 text-center text-sm text-muted-foreground">
          Backup &amp; restore controls are coming here.
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="border-b border-border bg-card">
          <div className="flex items-center gap-2">
            <Server className="h-5 w-5 text-muted-foreground" />
            <CardTitle className="text-base">{copy.title}</CardTitle>
          </div>
          <CardDescription>{copy.description}</CardDescription>
        </CardHeader>

        <CardContent className="flex flex-col gap-6 pt-6">
          {/* ── Targets ─────────────────────────────────────────────── */}
          <div className="flex flex-col gap-2">
            {targets.map((x) => (
              <div
                key={x.id}
                className={`flex items-center gap-3 rounded-md border p-2 ${
                  selected === x.id ? "border-primary" : "border-border"
                }`}
              >
                <button
                  type="button"
                  className="flex-1 text-left"
                  onClick={() => {
                    setSelected(x.id);
                    resetRunState();
                  }}
                >
                  <div className="text-sm font-medium">{x.label}</div>
                  <div className="font-mono-ui text-xs text-text-tertiary">
                    {x.user}@{x.host}:{x.port}
                  </div>
                </button>
                <Button
                  size="sm"
                  outlined
                  onClick={() => {
                    setDraft({
                      id: x.id,
                      label: x.label,
                      host: x.host,
                      user: x.user,
                      port: String(x.port),
                      identity_file: x.identity_file,
                      target_home: x.target_home,
                    });
                    setFormError(null);
                  }}
                >
                  {copy.editTarget}
                </Button>
                <Button size="sm" ghost onClick={() => void handleDelete(x.id)}>
                  <Trash2 className="h-4 w-4" />
                </Button>
              </div>
            ))}

            {draft === null && (
              <Button
                size="sm"
                outlined
                prefix={<Plus className="h-4 w-4" />}
                onClick={() => {
                  setDraft({ ...EMPTY_DRAFT });
                  setFormError(null);
                }}
              >
                {copy.addTarget}
              </Button>
            )}
          </div>

          {/* ── Target editor ───────────────────────────────────────── */}
          {draft !== null && (
            <div className="flex flex-col gap-3 rounded-md border border-border p-3">
              <div className="grid gap-3 sm:grid-cols-2">
                {(
                  [
                    ["id", copy.fieldId],
                    ["label", copy.fieldLabel],
                    ["host", copy.fieldHost],
                    ["user", copy.fieldUser],
                    ["port", copy.fieldPort],
                    ["identity_file", copy.fieldIdentityFile],
                    ["target_home", copy.fieldTargetHome],
                  ] as const
                ).map(([field, label]) => (
                  <div key={field} className="flex flex-col gap-1">
                    <Label htmlFor={`migration-${field}`}>{label}</Label>
                    <Input
                      id={`migration-${field}`}
                      value={draft[field] ?? ""}
                      onChange={(e) =>
                        setDraft({ ...draft, [field]: e.target.value })
                      }
                    />
                  </div>
                ))}
              </div>
              <p className="text-xs text-text-tertiary">{copy.identityFileHint}</p>
              {formError && (
                <span className="text-xs text-destructive">{formError}</span>
              )}
              <div className="flex gap-2">
                <Button size="sm" onClick={() => void handleSave()}>
                  {t.common.save}
                </Button>
                <Button size="sm" ghost onClick={() => setDraft(null)}>
                  {t.common.cancel}
                </Button>
              </div>
            </div>
          )}

          {/* ── Preflight ───────────────────────────────────────────── */}
          {target && (
            <div className="flex flex-col gap-3">
              <div className="flex items-center gap-2">
                <Button
                  size="sm"
                  outlined
                  disabled={preflighting}
                  prefix={preflighting ? <Spinner /> : undefined}
                  onClick={() => void handlePreflight()}
                >
                  {preflighting ? copy.preflightRunning : copy.preflight}
                </Button>
                {checks.length > 0 && (
                  <Badge tone={canStart ? "outline" : "destructive"}>
                    {canStart ? copy.preflightPassed : copy.preflightBlocked}
                  </Badge>
                )}
              </div>

              {preflightError && (
                <span className="text-xs text-destructive">{preflightError}</span>
              )}

              {checks.map((c) => {
                const tone = checkTone(c);
                const color =
                  tone === "ok"
                    ? "text-success"
                    : tone === "warn"
                      ? "text-warning"
                      : "text-destructive";
                return (
                  <div key={c.name} className="flex items-start gap-2 text-xs">
                    <span className={color}>
                      {tone === "ok" ? "✓" : tone === "warn" ? "⚠" : "✗"}
                    </span>
                    <span className="font-mono-ui">{c.name}</span>
                    <Badge tone="outline" className="text-xs">
                      {c.tier === "blocking" ? copy.tierBlocking : copy.tierWarning}
                    </Badge>
                    <span className="text-text-tertiary">{c.detail}</span>
                  </div>
                );
              })}

              {blockedByOverwrite && (
                <label className="flex items-start gap-2 rounded-md border border-destructive p-2 text-xs">
                  <Checkbox
                    checked={confirmOverwrite}
                    onCheckedChange={(v: boolean) => setConfirmOverwrite(Boolean(v))}
                  />
                  <span>
                    <span className="font-medium">{copy.confirmOverwrite}</span>
                    <br />
                    <span className="text-text-tertiary">
                      {copy.confirmOverwriteHint}
                    </span>
                  </span>
                </label>
              )}

              <div>
                <Button
                  size="sm"
                  disabled={!canStart || running}
                  prefix={running ? <Spinner /> : undefined}
                  onClick={() => void handleStart()}
                >
                  {running ? copy.starting : copy.start}
                </Button>
              </div>
            </div>
          )}

          {/* ── Execution ───────────────────────────────────────────── */}
          {running && (
            <div className="flex flex-col gap-3">
              <div className="h-2 w-full overflow-hidden rounded bg-midground">
                <div
                  className="h-full bg-primary transition-all"
                  style={{ width: `${percent}%` }}
                />
              </div>
              <ActionLogViewer
                action={MIGRATE_ACTION}
                onLines={handleLines}
                onClose={() => setRunning(false)}
                onComplete={() => setFinished(true)}
              />
            </div>
          )}

          {/* The archive holds plaintext .env and auth.json — if it could not
              be removed, that must not read as an ordinary log line. */}
          {cleanupWarning && (
            <div className="flex items-start gap-2 rounded-md border border-destructive bg-destructive/10 p-3 text-xs">
              <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0 text-destructive" />
              <span>{cleanupWarning}</span>
            </div>
          )}

          {/* ── Completion ──────────────────────────────────────────── */}
          {finished && target && (
            <div className="flex flex-col gap-2 rounded-md border border-border p-3">
              <div className="text-sm font-medium">{copy.doneTitle}</div>
              <p className="text-xs">{copy.doneNotStarted}</p>
              <CommandBlock
                label={copy.doneStartCommand}
                code={migrationStartCommand(target)}
              />
              <p className="text-xs text-text-tertiary">
                {copy.doneResidualCredentials}
              </p>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
