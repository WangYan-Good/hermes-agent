import { useCallback, useEffect, useRef, useState } from "react";
import {
  Archive,
  Database,
  Download,
  Plus,
  Server,
  Trash2,
  TriangleAlert,
  Upload,
} from "lucide-react";
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
import { ConfirmDialog } from "@nous-research/ui/ui/components/confirm-dialog";
import { Input } from "@nous-research/ui/ui/components/input";
import { Label } from "@nous-research/ui/ui/components/label";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { Toast } from "@nous-research/ui/ui/components/toast";
import { useToast } from "@nous-research/ui/hooks/use-toast";

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

type BackupImportTarget =
  | { kind: "upload"; file: File }
  | { kind: "path"; path: string };

function backupImportLabel(
  target: BackupImportTarget | null,
  fallback: string,
): string {
  if (!target) return fallback;
  return target.kind === "upload" ? target.file.name : target.path;
}

function backupFileName(path: string | null, fallback: string): string {
  if (!path) return fallback;
  return path.split(/[\\/]/).filter(Boolean).pop() ?? path;
}

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
  const bcopy = t.backupRestore;
  const { toast, showToast } = useToast();

  // ── Backup & restore ──────────────────────────────────────────────
  const [backupAction, setBackupAction] = useState<string | null>(null);
  const [pendingBackupArchive, setPendingBackupArchive] = useState<
    string | null
  >(null);
  const [downloadableBackupArchive, setDownloadableBackupArchive] = useState<
    string | null
  >(null);
  const [downloadingBackup, setDownloadingBackup] = useState(false);
  const importUploadInputRef = useRef<HTMLInputElement | null>(null);
  const [importFile, setImportFile] = useState<File | null>(null);
  const [importPath, setImportPath] = useState("");
  // Restore-from-backup is destructive (overwrites the live config) and the
  // spawned `hermes import` runs non-interactively (stdin is /dev/null), so
  // its CLI "Continue? [y/N]" prompt would auto-abort. The dashboard owns the
  // consent: confirm here, then call the endpoint with force=true.
  const [importingBackup, setImportingBackup] = useState(false);
  const [importConfirmTarget, setImportConfirmTarget] =
    useState<BackupImportTarget | null>(null);

  const runDashboardBackup = async () => {
    try {
      const res = await api.runBackup();
      setBackupAction(res.name);
      setPendingBackupArchive(res.archive ?? null);
      setDownloadableBackupArchive(null);
      showToast(bcopy.backupStarted, "success");
    } catch (e) {
      showToast(`${bcopy.backupFailed}: ${e}`, "error");
    }
  };

  const handleBackupActionComplete = useCallback(
    (action: string, exitCode: number | null) => {
      if (action === "backup" && pendingBackupArchive) {
        if (exitCode === 0) {
          setDownloadableBackupArchive(pendingBackupArchive);
          showToast(bcopy.backupReady, "success");
        } else {
          setPendingBackupArchive(null);
        }
      }
    },
    [pendingBackupArchive, showToast, bcopy.backupReady],
  );

  const downloadBackup = async () => {
    const archive = downloadableBackupArchive;
    if (!archive) return;
    setDownloadingBackup(true);
    try {
      const res = await api.downloadBackup(archive);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = backupFileName(archive, bcopy.noBackupYet);
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
    } catch (e) {
      showToast(`${bcopy.downloadFailed}: ${e}`, "error");
    } finally {
      setDownloadingBackup(false);
    }
  };

  const clearImportFile = () => {
    setImportFile(null);
    if (importUploadInputRef.current) importUploadInputRef.current.value = "";
  };

  const runBackupImport = async (target: BackupImportTarget) => {
    setImportingBackup(true);
    try {
      const res =
        target.kind === "upload"
          ? await api.runImportUpload(target.file, true)
          : await api.runImport(target.path, true);
      setBackupAction(res.name);
      showToast(bcopy.importStarted, "success");
      if (target.kind === "upload") clearImportFile();
    } catch (e) {
      showToast(`${bcopy.importFailed}: ${e}`, "error");
    } finally {
      setImportingBackup(false);
    }
  };

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
    let cancelled = false;
    api
      .listMigrationTargets()
      .then((response) => {
        if (!cancelled) setTargets(response.targets);
      })
      .catch((error) => {
        if (!cancelled) setFormError(String(error));
      });
    return () => {
      cancelled = true;
    };
  }, []);

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
      // The first preflight pins the host key, so the stored profile changed.
      await refresh();
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
      <Toast toast={toast} />
      <input
        ref={importUploadInputRef}
        type="file"
        accept=".zip,application/zip,application/x-zip-compressed"
        className="hidden"
        onChange={(event) => {
          setImportFile(event.currentTarget.files?.[0] ?? null);
        }}
      />

      <Card>
        <CardHeader className="border-b border-border bg-card">
          <div className="flex items-center gap-2">
            <Archive className="h-5 w-5 text-muted-foreground" />
            <CardTitle className="text-base">{bcopy.title}</CardTitle>
          </div>
          <CardDescription>{bcopy.description}</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-4 py-6">
          <div className="flex flex-col gap-3 lg:flex-row lg:items-end">
            <div className="grid min-w-0 flex-1 gap-2">
              <Label>{bcopy.fullBackupLabel}</Label>
              <div className="flex min-w-0 flex-col gap-2 sm:flex-row sm:items-center">
                <Button
                  size="sm"
                  ghost
                  prefix={<Database className="h-3.5 w-3.5" />}
                  onClick={() => void runDashboardBackup()}
                >
                  {bcopy.createBackup}
                </Button>
                <Button
                  size="sm"
                  ghost
                  disabled={!downloadableBackupArchive || downloadingBackup}
                  prefix={
                    downloadingBackup ? (
                      <Spinner className="h-3.5 w-3.5" />
                    ) : (
                      <Download className="h-3.5 w-3.5" />
                    )
                  }
                  onClick={() => void downloadBackup()}
                >
                  {bcopy.downloadBackup}
                </Button>
                <span
                  className="min-w-0 truncate text-xs text-muted-foreground"
                  title={pendingBackupArchive ?? bcopy.noBackupYet}
                >
                  {backupFileName(pendingBackupArchive, bcopy.noBackupYet)}
                </span>
              </div>
            </div>
          </div>

          <div className="flex flex-col gap-3 border-t border-border pt-4 sm:flex-row sm:items-end">
            <div className="grid min-w-0 flex-1 gap-2">
              <Label>{bcopy.restoreUploadLabel}</Label>
              <div className="flex min-w-0 flex-col gap-2 sm:flex-row sm:items-center">
                <Button
                  type="button"
                  size="sm"
                  ghost
                  disabled={importingBackup}
                  prefix={<Upload className="h-3.5 w-3.5" />}
                  onClick={() => importUploadInputRef.current?.click()}
                >
                  {bcopy.chooseRestoreZip}
                </Button>
                <span
                  className="min-w-0 truncate text-xs text-muted-foreground"
                  title={importFile?.name ?? bcopy.noArchiveSelected}
                >
                  {importFile?.name ?? bcopy.noArchiveSelected}
                </span>
              </div>
            </div>
            <Button
              size="sm"
              ghost
              disabled={!importFile || importingBackup}
              prefix={importingBackup ? <Spinner /> : undefined}
              onClick={() => {
                if (!importFile) return;
                setImportConfirmTarget({ kind: "upload", file: importFile });
              }}
            >
              {bcopy.restoreUpload}
            </Button>
          </div>

          <div className="flex flex-col gap-3 border-t border-border pt-4 sm:flex-row sm:items-end">
            <div className="grid min-w-0 flex-1 gap-2">
              <Label htmlFor="import-path">{bcopy.restorePathLabel}</Label>
              <Input
                id="import-path"
                value={importPath}
                onChange={(e) => setImportPath(e.target.value)}
                placeholder={bcopy.restorePathPlaceholder}
              />
            </div>
            <Button
              size="sm"
              ghost
              disabled={!importPath.trim() || importingBackup}
              prefix={importingBackup ? <Spinner /> : undefined}
              onClick={() => {
                const path = importPath.trim();
                if (!path) return;
                setImportConfirmTarget({ kind: "path", path });
              }}
            >
              {bcopy.restorePath}
            </Button>
          </div>

          {backupAction && (
            <ActionLogViewer
              action={backupAction}
              onComplete={handleBackupActionComplete}
              onClose={() => setBackupAction(null)}
            />
          )}

          <ConfirmDialog
            open={!!importConfirmTarget}
            title={bcopy.confirmRestoreTitle}
            description={bcopy.confirmRestoreDescription.replace(
              "{archive}",
              backupImportLabel(importConfirmTarget, bcopy.archiveFallback),
            )}
            destructive
            confirmLabel={bcopy.confirmRestoreConfirm}
            cancelLabel={t.common.cancel}
            onCancel={() => setImportConfirmTarget(null)}
            onConfirm={() => {
              const target = importConfirmTarget;
              setImportConfirmTarget(null);
              if (target) void runBackupImport(target);
            }}
          />
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
                  {/* The pin is what makes a later host-key change an error
                      rather than something silently accepted. */}
                  <div className="font-mono-ui text-xs text-text-tertiary">
                    {x.host_fingerprint
                      ? `${copy.hostKeyPinned} ${x.host_fingerprint}`
                      : copy.hostKeyUnpinned}
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
