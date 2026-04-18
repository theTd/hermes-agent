import { Badge } from "@/components/ui/badge";
import type { LiveTraceGroup } from "../types";
import type { TraceLanePreviewViewModel } from "../domain/monitor-view";

function fmtTime(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString();
}

function fmtDuration(durationMs: number | null): string {
  if (durationMs == null) return "live";
  return durationMs < 1000 ? `${durationMs.toFixed(0)}ms` : `${(durationMs / 1000).toFixed(1)}s`;
}

function variantForStatus(status: string): "destructive" | "success" | "warning" | "outline" {
  if (status === "error") {
    return "destructive";
  }
  if (status === "completed") {
    return "success";
  }
  if (status === "rejected" || status === "ignored") {
    return "warning";
  }
  return "outline";
}

export function TraceGroupCard({
  group,
  selected,
  onSelect,
  lanePreviews,
}: {
  group: LiveTraceGroup;
  selected: boolean;
  onSelect: () => void;
  lanePreviews?: TraceLanePreviewViewModel[];
}) {
  const previews = lanePreviews ?? group.timeline.lanes.slice(0, 3).map((lane) => ({
    key: lane.key,
    title: lane.title,
    model: lane.model,
    requestCount: lane.requestBlocks.length,
    lastSummary: lane.timelineEntries[lane.timelineEntries.length - 1]?.summary || "No lane activity yet.",
  }));

  return (
    <button
      type="button"
      onClick={onSelect}
      className={`block w-full rounded-xl border p-3 text-left transition-colors ${
        selected
          ? "border-primary/60 bg-card/85"
          : "border-border/30 bg-card/25 hover:bg-card/45"
      }`}
    >
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant={variantForStatus(group.status) as any}>{group.status}</Badge>
        {group.activeStream && <Badge variant={"outline" as any}>live</Badge>}
        <span className="text-[10px] font-mono-ui uppercase tracking-wider opacity-50">
          {fmtTime(group.startedAt)}
        </span>
        <span className="text-[10px] font-mono-ui uppercase tracking-wider opacity-50">
          {fmtDuration(group.durationMs)}
        </span>
      </div>

      <div className="mt-3 text-sm leading-6">{group.headline}</div>

      <div className="mt-3 flex flex-wrap gap-x-3 gap-y-1 text-[10px] font-mono-ui uppercase tracking-wider opacity-60">
        <span>{group.timeline.requestCount} llm requests</span>
        <span>{group.timeline.laneCount} lanes</span>
        <span>{group.eventCount} events</span>
        <span>{group.latestStage}</span>
      </div>

      {previews.length > 0 && (
        <div className="mt-3 space-y-2">
          {previews.map((lane) => (
            <div
              key={lane.key}
              className="rounded-lg border border-border/20 bg-background/30 px-2 py-2 text-xs"
            >
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-mono-ui opacity-80">{lane.title}</span>
                {lane.model && <span className="font-mono-ui opacity-50">{lane.model}</span>}
                <span className="font-mono-ui opacity-50">{lane.requestCount} req</span>
              </div>
              <div className="mt-1 opacity-70">
                {lane.lastSummary}
              </div>
            </div>
          ))}
        </div>
      )}
    </button>
  );
}
