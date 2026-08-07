import { useEffect, useRef, useState } from "react";
import { Terminal, X } from "lucide-react";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { Card, CardContent } from "@nous-research/ui/ui/components/card";
import { api } from "@/lib/api";

/**
 * Live action-log viewer for the spawn-based admin actions (doctor, audit,
 * backup, import, skills update, checkpoints prune, gateway start/stop,
 * migrate-host, ...). Polls /api/actions/<name>/status until the process
 * exits. Shared by SystemPage (ops) and MigratePage (backup/restore).
 */
export function ActionLogViewer({
  action,
  onClose,
  onComplete,
  onLines,
}: {
  action: string;
  onClose: () => void;
  onComplete?: (action: string, exitCode: number | null) => void;
  /** Fired with the full line buffer on every poll tick, so a caller that
   *  needs to derive structure from the log (e.g. MigratePage parsing
   *  `[stage] status detail` lines for a progress bar) can piggyback on this
   *  component's existing poll instead of starting a second one. */
  onLines?: (lines: string[]) => void;
}) {
  const [lines, setLines] = useState<string[]>([]);
  const [running, setRunning] = useState(true);
  const [exitCode, setExitCode] = useState<number | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const completeRef = useRef(false);

  // Held in refs, and updated in an effect rather than during render, so the
  // poll loop always calls the CURRENT callback. Capturing them in the poll
  // closure instead would pin whichever callback existed on first render —
  // a stale-closure bug that only shows up once the parent re-renders with a
  // new handler, which MigratePage does on every log tick.
  const onLinesRef = useRef(onLines);
  const onCompleteRef = useRef(onComplete);
  useEffect(() => {
    onLinesRef.current = onLines;
    onCompleteRef.current = onComplete;
  }, [onLines, onComplete]);

  useEffect(() => {
    let cancelled = false;
    completeRef.current = false;
    const poll = async () => {
      try {
        const st = await api.getActionStatus(action, 400);
        if (cancelled) return;
        setLines(st.lines);
        setRunning(st.running);
        setExitCode(st.exit_code);
        onLinesRef.current?.(st.lines);
        if (!st.running && !completeRef.current) {
          completeRef.current = true;
          onCompleteRef.current?.(action, st.exit_code);
        }
        if (st.running) timer.current = setTimeout(poll, 1200);
      } catch {
        if (!cancelled) setRunning(false);
      }
    };
    poll();
    return () => {
      cancelled = true;
      if (timer.current) clearTimeout(timer.current);
    };
     
  }, [action]);

  return (
    <Card>
      <CardContent className="py-4">
        <div className="flex items-center justify-between mb-2">
          <div className="flex items-center gap-2">
            <Terminal className="h-4 w-4 text-muted-foreground" />
            <span className="font-mono text-sm">{action}</span>
            {running ? (
              <Badge tone="warning">running</Badge>
            ) : (
              <Badge tone={exitCode === 0 ? "success" : "destructive"}>
                {exitCode === 0 ? "done" : `exit ${exitCode}`}
              </Badge>
            )}
          </div>
          <Button ghost size="icon" onClick={onClose} aria-label="Close log">
            <X />
          </Button>
        </div>
        <pre className="max-h-72 overflow-auto whitespace-pre-wrap break-words bg-background/50 border border-border p-3 text-xs font-mono text-muted-foreground">
          {lines.length ? lines.join("\n") : "Starting…"}
        </pre>
      </CardContent>
    </Card>
  );
}
