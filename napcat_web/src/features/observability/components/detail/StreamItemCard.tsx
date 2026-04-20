import { useEffect, useRef, useState } from "react";
import { Badge } from "@/components/ui/badge";
import type { TraceTimelineItemViewModel } from "../../domain/detail-view";
import { RequestBlockCard } from "./RequestBlockCard";
import { TimelineEventCard } from "./TimelineEventCard";
import { fmtDuration, fmtTime, previewText } from "./format";

function getItemTimestamp(item: TraceTimelineItemViewModel): number {
  if (item.kind === "request" && item.block) {
    return item.block.startedAt;
  }
  if (item.kind === "event" && item.entry) {
    return item.entry.ts;
  }
  return 0;
}

function getItemStatus(item: TraceTimelineItemViewModel): string {
  if (item.kind === "request" && item.block) {
    return item.block.status;
  }
  if (item.kind === "event" && item.entry) {
    return item.entry.status;
  }
  return "";
}

function getItemTitle(item: TraceTimelineItemViewModel): string {
  if (item.kind === "request" && item.block) {
    return item.block.model ? `${item.block.model} request` : "LLM request";
  }
  if (item.kind === "event" && item.entry) {
    return item.entry.title;
  }
  return "";
}

function StatusDot({ status }: { status: string }) {
  if (status === "running") {
    return (
      <span className="relative flex h-3 w-3">
        <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-sky-400 opacity-75" />
        <span className="relative inline-flex rounded-full h-3 w-3 bg-sky-500" />
      </span>
    );
  }
  if (status === "completed" || status === "success") {
    return <span className="inline-block h-3 w-3 rounded-full bg-emerald-500" />;
  }
  if (status === "failed" || status === "error") {
    return <span className="inline-block h-3 w-3 rounded-full bg-red-500" />;
  }
  return <span className="inline-block h-3 w-3 rounded-full bg-muted-foreground/40" />;
}

export function StreamItemCard({
  item,
  onOpenFullscreen,
  isLast,
  shareOfTotal = 0,
  depth = 0,
  hasChildren = false,
  isExpanded = true,
  onToggleExpand,
}: {
  item: TraceTimelineItemViewModel;
  onOpenFullscreen: (block: { label: string; content: string; title?: string }) => void;
  isLast: boolean;
  shareOfTotal?: number;
  depth?: number;
  hasChildren?: boolean;
  isExpanded?: boolean;
  onToggleExpand?: () => void;
}) {
  const status = getItemStatus(item);
  const isRunning = status === "running";
  const [expanded, setExpanded] = useState(isRunning);
  const timestamp = getItemTimestamp(item);
  const title = getItemTitle(item);

  const prevStatusRef = useRef(status);
  useEffect(() => {
    if (prevStatusRef.current === "running" && status !== "running") {
      setExpanded(false);
    }
    prevStatusRef.current = status;
  }, [status]);

  const showFlameBar = shareOfTotal > 0.005;
  const flameWidthPct = Math.max(shareOfTotal * 100, 0.5);
  const indentPx = depth * 20;

  return (
    <div className="flex gap-3" style={{ marginLeft: indentPx }}>
      <div className="flex flex-col items-center pt-1">
        <StatusDot status={status} />
        {!isLast && <div className="w-px flex-1 bg-border/30 mt-1" />}
      </div>

      <div className="flex-1 min-w-0 pb-5 relative">
        {/* Flame-chart background bar showing time share */}
        {showFlameBar && (
          <div
            className="absolute top-0 left-0 h-full rounded bg-primary/[0.04] pointer-events-none"
            style={{ width: `${flameWidthPct}%` }}
          />
        )}

        <div className="flex items-center gap-2 flex-wrap relative">
          {hasChildren && onToggleExpand && (
            <button
              type="button"
              onClick={onToggleExpand}
              className="text-[10px] font-mono-ui opacity-60 hover:opacity-100 w-4 text-center shrink-0"
              title={isExpanded ? "Collapse children" : "Expand children"}
            >
              {isExpanded ? "▼" : "▶"}
            </button>
          )}
          <span className="text-xs font-medium">{title}</span>
          <Badge
            variant={(isRunning ? "default" : status === "failed" || status === "error" ? "destructive" : "outline") as any}
            className="text-[10px] px-1.5 py-0 h-4"
          >
            {status || "pending"}
          </Badge>
          <span className="text-[10px] font-mono-ui opacity-50">{fmtTime(timestamp)}</span>
          {item.kind === "request" && item.block?.durationMs != null && item.block.durationMs >= 100 && (
            <span className="text-[10px] font-mono-ui opacity-50">{fmtDuration(item.block.durationMs)}</span>
          )}
          {item.kind === "event" && item.entry?.durationMs != null && item.entry.durationMs >= 100 && (
            <span className="text-[10px] font-mono-ui opacity-50">{fmtDuration(item.entry.durationMs)}</span>
          )}
          {showFlameBar && (
            <span className="text-[10px] font-mono-ui opacity-40">
              {shareOfTotal >= 0.01 ? `${(shareOfTotal * 100).toFixed(0)}%` : "<1%"}
            </span>
          )}
          <button
            type="button"
            onClick={() => setExpanded((v) => !v)}
            className="ml-auto text-[10px] font-mono-ui opacity-60 hover:opacity-100"
          >
            {expanded ? "collapse" : "expand"}
          </button>
        </div>

        {item.kind === "request" && item.block && (
          <div className="mt-1 text-xs opacity-70 truncate relative">
            {item.block.responseText
              ? previewText(item.block.responseText, 140)
              : item.block.thinkingText
                ? previewText(item.block.thinkingText, 140)
                : "Waiting for response..."}
          </div>
        )}
        {item.kind === "event" && item.entry && (
          <div className="mt-1 text-xs opacity-70 relative">{item.entry.summary}</div>
        )}

        {expanded && (
          <div className="mt-2 relative">
            {item.kind === "request" && item.block && (
              <RequestBlockCard block={item.block} onOpenFullscreen={onOpenFullscreen} />
            )}
            {item.kind === "event" && item.entry && (
              <TimelineEventCard entry={item.entry} onOpenFullscreen={onOpenFullscreen} />
            )}
          </div>
        )}
      </div>
    </div>
  );
}
