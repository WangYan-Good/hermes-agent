import type { NativeControl } from "./native-control";
export function NativeActivity({ control }: { control: NativeControl }) {
  const labels: Record<string, string> = { calls: "Calls", input: "Input tokens", output: "Output tokens", total: "Total tokens", context_percent: "Context %", context_used: "Context tokens", context_max: "Context window", cost_usd: "Cost (USD)" };
  return <aside className="mx-auto w-full max-w-3xl px-5 py-2 text-sm" aria-label="Agent activity">
    {control.notice ? <p role="status">{control.notice}</p> : null}{control.queued ? <p role="status">Queued for next turn: {control.queued}</p> : null}
    {control.todos.length ? <details><summary>Tasks</summary><ul>{control.todos.map(t => <li key={t.id}>{t.status.replaceAll("_", " ")}: {t.content}</li>)}</ul></details> : null}
    {Object.values(control.subagents).map(child => <div key={child.id} className="my-1"><strong>{child.id}</strong> · {child.status}<p>{child.goal}</p><p className="truncate opacity-65">{child.activity}</p></div>)}
    {Object.keys(control.usage).length ? <dl className="flex flex-wrap gap-x-4 opacity-60">{Object.entries(control.usage).map(([key, value]) => <div className="flex gap-1" key={key}><dt>{labels[key]}:</dt><dd>{value}</dd></div>)}</dl> : null}
  </aside>;
}
