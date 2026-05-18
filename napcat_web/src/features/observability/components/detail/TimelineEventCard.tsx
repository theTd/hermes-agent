import { Badge } from "@/components/ui/badge";
import type { TimelineEntry } from "../../types";
import { ExpandableTextPanel } from "./ExpandableTextPanel";
import { fmtDuration, fmtTime, getExpandableTextPanelVisibility } from "./format";

export function TimelineEventCard({
  entry,
  onOpenFullscreen,
}: {
  entry: TimelineEntry;
  onOpenFullscreen: (block: { label: string; content: string; title?: string }) => void;
}) {
  return (
    <div className="border-b border-border/10 px-3 py-2 last:border-b-0">
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant={(entry.status === "error" ? "destructive" : "outline") as any} className="text-[10px] px-1.5 py-0 h-4">
          {entry.kind}
        </Badge>
        <span className="text-[10px] font-mono-ui uppercase tracking-wider opacity-50">
          {fmtTime(entry.ts)}
        </span>
        {entry.durationMs != null && entry.durationMs >= 100 && (
          <span className="text-[10px] font-mono-ui uppercase tracking-wider opacity-50">
            {fmtDuration(entry.durationMs)}
          </span>
        )}
      </div>
      <div className="mt-1 text-xs leading-5">{entry.summary}</div>
      {entry.payloadPreview && (
        <div className="mt-1">
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
