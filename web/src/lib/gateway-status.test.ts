import { describe, expect, it } from "vitest";

import { en } from "@/i18n/en";
import { gatewayLine } from "@/lib/gateway-status";

describe("gatewayLine", () => {
  it.each([
    ["running", false, "Running", "text-success"],
    ["starting", false, "Starting", "text-warning"],
    ["startup_failed", false, "Start failed", "text-destructive"],
    ["stopped", true, "Stopped", "text-muted-foreground"],
    [null, true, "Running", "text-success"],
    [null, false, "Off", "text-muted-foreground"],
  ] as const)(
    "maps state %s and running=%s to its translated status",
    (gatewayState, gatewayRunning, label, tone) => {
      expect(
        gatewayLine(
          { gateway_running: gatewayRunning, gateway_state: gatewayState },
          en,
        ),
      ).toEqual({ label, tone });
    },
  );
});
