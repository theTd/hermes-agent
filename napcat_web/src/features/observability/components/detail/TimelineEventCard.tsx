import { Badge } from "@/components/ui/badge";
import type { TimelineEntry } from "../../types";
import { ExpandableTextPanel } from "./ExpandableTextPanel";
import { fmtDuration, fmtTime, getExpandableTextPanelVisibility } from "./format";

export function TimelineEventCard({
  entry,
  onOpenFullscreen,
}: {
  entry: TimelineEntry;
  onOpenFullscreen: (block: { label: string; content: string }) => void;
}) {
  return (
    <div className="rounded-2xl border border-border/30 bg-card/20 p-3">
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant={(entry.status === "error" ? "destructive" : "outline") as any}>
          {entry.kind}
        </Badge>
        <span className="text-[10px] font-mono-ui uppercase tracking-wider opacity-50">
          {fmtTime(entry.ts)}
        </span>
        {entry.durationMs != null && (
          <span className="text-[10px] font-mono-ui uppercase tracking-wider opacity-50">
            {fmtDuration(entry.durationMs)}
          </span>
        )}
      </div>
      <div className="mt-3 text-sm leading-6">{entry.summary}</div>
      {entry.payloadPreview && (
        <div className="mt-3">
          <ExpandableTextPanel
            title="Debug Payload"
            label={entry.title}
            content={entry.payloadPreview}
            visibility={getExpandableTextPanelVisibility({
              title: "Debug Payload",
              label: entry.title,
              content: entry.payloadPreview,
            })}
            onOpenFullscreen={onOpenFullscreen}
          />
        </div>
      )}
    </div>
  );
}
