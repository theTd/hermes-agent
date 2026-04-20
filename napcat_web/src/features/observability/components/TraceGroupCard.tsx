import type { LiveTraceGroup } from "../types";
import type { TraceLanePreviewViewModel } from "../domain/monitor-view";

function fmtTime(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString();
}

function fmtDuration(durationMs: number | null): string {
  if (durationMs == null) return "live";
  return durationMs < 1000 ? `${durationMs.toFixed(0)}ms` : `${(durationMs / 1000).toFixed(1)}s`;
}

function statusDotClass(status: string): string {
  if (status === "error") return "bg-destructive";
  if (status === "completed") return "bg-success";
  if (status === "rejected" || status === "ignored") return "bg-warning";
  return "bg-muted-foreground/50";
}

function cardTitleForGroup(group: LiveTraceGroup): string {
  const inboundEvent = group.rawEvents.find((event) => event.event_type === "message.received");
  if (inboundEvent) {
    const payload = inboundEvent.payload ?? {};
    const inboundText = String(
      payload.text_preview
      || payload.text
      || payload.raw_message
      || "",
    ).trim();
    if (inboundText) {
      return inboundText;
    }
  }
  return group.headline || "No headline";
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
  const cardTitle = cardTitleForGroup(group);
  const previews = lanePreviews ?? group.timeline.lanes.slice(0, 2).map((lane) => ({
    key: lane.key,
    title: lane.title,
    model: lane.model,
    requestCount: lane.requestBlocks.length,
    lastSummary: lane.timelineEntries[lane.timelineEntries.length - 1]?.summary || "",
  }));

  return (
    <button
      type="button"
      onClick={onSelect}
      className={`block w-full text-left transition-colors ${
        selected
          ? "bg-primary/10"
          : "hover:bg-card/40"
      }`}
    >
      <div className="flex items-center gap-2 px-3 py-1.5">
        <div className={`h-2 w-2 shrink-0 rounded-full ${statusDotClass(group.status)}`} />
        <span className="w-16 shrink-0 text-[10px] font-mono-ui opacity-60">
          {fmtTime(group.startedAt)}
        </span>
        <span className="w-12 shrink-0 text-[10px] font-mono-ui opacity-50">
          {fmtDuration(group.durationMs)}
        </span>
        <span className="min-w-0 flex-1 truncate text-xs" title={cardTitle}>
          {cardTitle}
        </span>
        <span className="w-8 shrink-0 text-right text-[10px] font-mono-ui opacity-50">
          {group.eventCount}
        </span>
        <span className="w-8 shrink-0 text-right text-[10px] font-mono-ui opacity-50">
          {group.timeline.requestCount}
        </span>
        {group.errorCount > 0 && (
          <span className="w-6 shrink-0 text-right text-[10px] font-mono-ui text-destructive">
            {group.errorCount}
          </span>
        )}
      </div>

      {previews.length > 0 && (
        <div className="flex flex-wrap gap-x-3 gap-y-0.5 px-3 pb-1.5 pl-[5.5rem]">
          {previews.map((lane) => (
            <span key={lane.key} className="text-[10px] font-mono-ui opacity-50" title={lane.lastSummary}>
              {lane.title}
              {lane.model ? ` · ${lane.model}` : ""}
              {lane.requestCount > 0 ? ` · ${lane.requestCount}req` : ""}
            </span>
          ))}
        </div>
      )}
    </button>
  );
}
