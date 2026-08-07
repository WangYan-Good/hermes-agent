import { describe, expect, it } from "vitest";
import {
  MIGRATION_STAGES,
  canStartMigration,
  checkTone,
  isBlocked,
  isHostKeyMismatchError,
  migrationStartCommand,
  needsOverwriteConfirm,
  parseActionLine,
  stageProgress,
  toTargetPayload,
  validateTargetDraft,
} from "./migration";

describe("checkTone", () => {
  it("maps a failed blocking check to error", () => {
    expect(checkTone({ name: "python3", tier: "blocking", ok: false, detail: "" }))
      .toBe("error");
  });

  it("maps a failed warning check to warn, not error", () => {
    // A warning must never look like a stop: clock skew is worth seeing but
    // does not prevent the migration.
    expect(checkTone({ name: "clock_skew", tier: "warning", ok: false, detail: "" }))
      .toBe("warn");
  });

  it("maps any passing check to ok", () => {
    expect(checkTone({ name: "os", tier: "blocking", ok: true, detail: "" })).toBe("ok");
    expect(checkTone({ name: "clock_skew", tier: "warning", ok: true, detail: "" }))
      .toBe("ok");
  });
});

describe("isBlocked", () => {
  it("is true only when a blocking check failed", () => {
    expect(isBlocked([{ name: "a", tier: "warning", ok: false, detail: "" }])).toBe(false);
    expect(isBlocked([{ name: "b", tier: "blocking", ok: false, detail: "" }])).toBe(true);
  });

  it("is false for an empty list", () => {
    expect(isBlocked([])).toBe(false);
  });
});

describe("stageProgress", () => {
  it("reports 0 before anything starts", () => {
    expect(stageProgress(null, "start")).toBe(0);
  });

  it("increases monotonically across the stage order", () => {
    const seen = MIGRATION_STAGES.map((s) => stageProgress(s, "ok"));
    for (let i = 1; i < seen.length; i += 1) {
      expect(seen[i]).toBeGreaterThan(seen[i - 1]);
    }
  });

  it("reaches 100 only when the last stage completes", () => {
    expect(stageProgress("verify", "ok")).toBe(100);
    expect(stageProgress("verify", "start")).toBeLessThan(100);
  });

  it("does not advance past a failure", () => {
    expect(stageProgress("transfer", "fail")).toBe(stageProgress("transfer", "start"));
  });

  it("handles cleanup notice without advancing progress", () => {
    // Cleanup is not a progress stage; it must not crash and must not advance
    expect(stageProgress("cleanup", "ok")).toBe(0);
    expect(stageProgress("cleanup", "start")).toBe(0);
  });
});

describe("parseActionLine", () => {
  it("parses the CLI's emit format", () => {
    expect(parseActionLine("[transfer] ok 12345 bytes")).toEqual({
      stage: "transfer",
      status: "ok",
      detail: "12345 bytes",
    });
  });

  it("tolerates a missing detail", () => {
    expect(parseActionLine("[install] start")).toEqual({
      stage: "install",
      status: "start",
      detail: "",
    });
  });

  it("returns null for unrelated log noise", () => {
    // The log tail carries installer output too; only our own lines count.
    expect(parseActionLine("Downloading hermes...")).toBeNull();
    expect(parseActionLine("[not-a-stage] ok")).toBeNull();
  });

  it("parses a cleanup notice as a distinct warning", () => {
    // Cleanup is emitted when the remote archive cannot be deleted.
    // It must be recognizable as a notice (not a progress stage).
    const result = parseActionLine("[cleanup] warn failed to remove /tmp/hermes-migration.zip on the target");
    expect(result).not.toBeNull();
    expect(result?.stage).toBe("cleanup");
    expect(result?.status).toBe("warn");
    expect(result?.detail).toMatch(/failed to remove/);
  });

  it("distinguishes cleanup notice from stage events", () => {
    // The caller must identify cleanup as a notice without string-sniffing.
    const stageEvent = parseActionLine("[transfer] ok 12345 bytes");
    const cleanupNotice = parseActionLine("[cleanup] warn failed to remove");
    expect(stageEvent?.stage === "cleanup").toBe(false);
    expect(cleanupNotice?.stage === "cleanup").toBe(true);
  });
});

describe("needsOverwriteConfirm", () => {
  it("is false when nothing is blocking", () => {
    expect(needsOverwriteConfirm([{ name: "os", tier: "blocking", ok: true, detail: "" }]))
      .toBe(false);
  });

  it("is true when target_home is the only blocking failure", () => {
    expect(needsOverwriteConfirm([
      { name: "target_home", tier: "blocking", ok: false, detail: "" },
      { name: "clock_skew", tier: "warning", ok: false, detail: "" },
    ])).toBe(true);
  });

  it("is false when a non-target_home check is also blocking", () => {
    // python3 missing is not something an overwrite checkbox can waive.
    expect(needsOverwriteConfirm([
      { name: "target_home", tier: "blocking", ok: false, detail: "" },
      { name: "python3", tier: "blocking", ok: false, detail: "" },
    ])).toBe(false);
  });
});

describe("canStartMigration", () => {
  it("is enabled when nothing blocks, regardless of the checkbox", () => {
    expect(canStartMigration([{ name: "os", tier: "blocking", ok: true, detail: "" }], false))
      .toBe(true);
  });

  it("stays disabled for a target_home block until the checkbox is ticked", () => {
    const checks = [{ name: "target_home", tier: "blocking" as const, ok: false, detail: "" }];
    expect(canStartMigration(checks, false)).toBe(false);
    expect(canStartMigration(checks, true)).toBe(true);
  });

  it("cannot be overridden by the checkbox when another blocking check also failed", () => {
    const checks = [
      { name: "target_home", tier: "blocking" as const, ok: false, detail: "" },
      { name: "disk_space", tier: "blocking" as const, ok: false, detail: "" },
    ];
    expect(canStartMigration(checks, true)).toBe(false);
  });
});

describe("isHostKeyMismatchError", () => {
  it("recognizes ssh's own host-key-mismatch stderr", () => {
    expect(isHostKeyMismatchError("Host key verification failed.")).toBe(true);
    expect(isHostKeyMismatchError("502: REMOTE HOST IDENTIFICATION HAS CHANGED! ... Host key verification failed."))
      .toBe(true);
  });

  it("does not flag an unrelated failure", () => {
    expect(isHostKeyMismatchError("Connection timed out")).toBe(false);
    expect(isHostKeyMismatchError("404: unknown target prod")).toBe(false);
  });
});

describe("migrationStartCommand", () => {
  it("builds a plain ssh command for the default port with no identity file", () => {
    expect(migrationStartCommand({
      host: "example.com", user: "hermes", port: 22, identity_file: "", target_home: "",
    })).toBe('ssh hermes@example.com "HERMES_HOME=~/.hermes hermes gateway start"');
  });

  it("includes -p for a non-default port and -i for an identity file", () => {
    expect(migrationStartCommand({
      host: "10.0.0.5", user: "root", port: 2222,
      identity_file: "~/.ssh/id_migration", target_home: "/opt/hermes-home",
    })).toBe(
      'ssh -p 2222 -i ~/.ssh/id_migration root@10.0.0.5 "HERMES_HOME=/opt/hermes-home hermes gateway start"',
    );
  });
});

describe("toTargetPayload", () => {
  it("trims required fields and omits blank optional ones", () => {
    expect(toTargetPayload({
      id: " prod ", host: " h ", user: " u ",
      label: "", identity_file: "", target_home: "", port: "",
    })).toEqual({ id: "prod", host: "h", user: "u" });
  });

  it("includes optional fields when present, converting port to a number", () => {
    expect(toTargetPayload({
      id: "prod", host: "h", user: "u", label: "Prod box",
      identity_file: "~/.ssh/id_ed25519", target_home: "/opt/hermes-home",
      port: "2222",
    })).toEqual({
      id: "prod", host: "h", user: "u", label: "Prod box",
      identity_file: "~/.ssh/id_ed25519", target_home: "/opt/hermes-home",
      port: 2222,
    });
  });

  it("never sends a blank port — the server can't coerce '' to int", () => {
    const payload = toTargetPayload({ id: "a", host: "h", user: "u", port: "" });
    expect(payload).not.toHaveProperty("port");
  });
});

describe("validateTargetDraft", () => {
  it("accepts a minimal draft", () => {
    expect(validateTargetDraft({ id: "prod", host: "h", user: "u" })).toBeNull();
  });

  it("rejects a non-slug id", () => {
    expect(validateTargetDraft({ id: "Prod Box", host: "h", user: "u" })).toMatch(/id/);
  });

  it("requires host and user", () => {
    expect(validateTargetDraft({ id: "a", host: "", user: "u" })).toMatch(/host/);
    expect(validateTargetDraft({ id: "a", host: "h", user: "" })).toMatch(/user/);
  });

  it("rejects an out-of-range port", () => {
    expect(validateTargetDraft({ id: "a", host: "h", user: "u", port: "70000" }))
      .toMatch(/port/);
  });

  it("rejects password authentication", () => {
    // Password must never be accepted: the server mirrors this check.
    expect(validateTargetDraft({ id: "a", host: "h", user: "u", password: "secret" }))
      .toMatch(/password/);
  });
});
