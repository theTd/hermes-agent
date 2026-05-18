import { Link } from "react-router-dom";
import type { NapcatTrace } from "@/lib/napcat-api";

function fmtTime(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString();
}

export function RecentErrorsTable({ traces }: { traces: NapcatTrace[] }) {
  if (!traces.length) {
    return (
      <div className="rounded bg-background/20 p-3">
        <div className="text-[10px] font-mono-ui uppercase tracking-wider text-muted-foreground/60">
          Recent Errors
        </div>
        <div className="mt-2 text-xs font-mono-ui opacity-40">no errors</div>
      </div>
    );
  }

  return (
    <div className="rounded bg-background/20 p-3">
      <div className="text-[10px] font-mono-ui uppercase tracking-wider text-muted-foreground/60">
        Recent Errors
      </div>
      <div className="mt-2 divide-y divide-border/10">
        {traces.map((trace) => (
          <Link
            key={trace.trace_id}
            to={`/traces/${trace.trace_id}`}
            className="flex items-center gap-3 py-1.5 transition-colors hover:bg-background/20"
          >
            <span className="w-16 shrink-0 text-[10px] font-mono-ui opacity-50">
              {fmtTime(trace.started_at)}
            </span>
            <span className="w-20 shrink-0 text-[10px] font-mono-ui text-destructive">
              {trace.status}
            </span>
            <span className="min-w-0 flex-1 truncate text-xs">
              {trace.summary?.headline || trace.trace_id.slice(0, 12)}
            </span>
            <span className="w-8 shrink-0 text-right text-[10px] font-mono-ui opacity-50">
              {trace.event_count}
            </span>
          </Link>
        ))}
      </div>
    </div>
  );
}
