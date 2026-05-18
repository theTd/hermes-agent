import { Link } from "react-router-dom";
import { Badge } from "@/components/ui/badge";
import type { LiveTraceGroup } from "../../types";
import { fmtDuration } from "./format";

export function TracePageHeader({ group }: { group: LiveTraceGroup }) {
  return (
    <div className="flex flex-wrap items-center gap-3 border-b border-border/30 px-4 py-3">
      <Link to="/" className="text-xs font-mono-ui opacity-60 hover:opacity-100">
        ← Back to console
      </Link>
      <span className="text-lg font-semibold tracking-wide" title={group.traceId}>Trace {group.traceId.slice(0, 12)}…</span>
      <Badge variant={(group.status === "error" ? "destructive" : group.status === "completed" ? "success" : group.status === "rejected" || group.status === "ignored" ? "warning" : "outline") as any}>
        {group.status}
      </Badge>
      <Badge variant={(group.activeStream ? "success" : "outline") as any}>
        {group.activeStream ? "LIVE" : "STALE"}
      </Badge>
      <span className="text-xs font-mono-ui opacity-50">
        {fmtDuration(group.durationMs)} · {group.eventCount} events
      </span>
    </div>
  );
}
