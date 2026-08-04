import { describe, expect, it } from "vitest";
import {
  MIGRATION_STAGES,
  checkTone,
  isBlocked,
  parseActionLine,
  stageProgress,
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
    expect(stageProgress("cleanup" as any, "ok")).toBe(0);
    expect(stageProgress("cleanup" as any, "start")).toBe(0);
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
