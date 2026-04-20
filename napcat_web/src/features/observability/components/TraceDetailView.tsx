import { useEffect, useState, useMemo, useRef } from "react";
import { InspectorSection } from "./InspectorSection";
import { ExpandableTextPanel } from "./detail/ExpandableTextPanel";
import { getExpandableTextPanelVisibility } from "./detail/format";
import { RawEventsPanel } from "./detail/RawEventsPanel";
import { TextBlockOverlay } from "./detail/TextBlockOverlay";
import { TraceOverviewPanel } from "./detail/TraceOverviewPanel";
import { StreamTimeline } from "./detail/StreamTimeline";
import { buildTraceDetailViewModel } from "../domain/detail-view";
import type { ObservabilityController } from "../types";

export function TraceDetailView({ controller }: { controller: ObservabilityController }) {
  const group = controller.selectedGroup;
  const [showRawEvents, setShowRawEvents] = useState(false);
  const [activeTextBlock, setActiveTextBlock] = useState<{ label: string; content: string; title?: string } | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const prevTraceIdRef = useRef<string | null>(null);

  const viewModel = useMemo(() => (group ? buildTraceDetailViewModel(group) : null), [group]);

  // 自动滚动到正在执行的条目（切换 trace 时）
  useEffect(() => {
    if (!viewModel || !scrollRef.current || !group) return;

    const traceId = group.traceId;
    if (prevTraceIdRef.current === traceId) return;
    prevTraceIdRef.current = traceId;

    const items = viewModel.lanes.flatMap((lane) => lane.timelineItems);
    const firstRunning = items.findIndex((item) => {
      if (item.kind === "request" && item.block) return item.block.status === "running";
      if (item.kind === "event" && item.entry) return item.entry.status === "running";
      return false;
    });

    // 如果没有 running，找最后一个条目；否则找第一个 running
    const sorted = [...items].sort((a, b) => {
      const tsA = a.kind === "request" ? (a.block?.startedAt ?? 0) : (a.entry?.ts ?? 0);
      const tsB = b.kind === "request" ? (b.block?.startedAt ?? 0) : (b.entry?.ts ?? 0);
      return tsA - tsB;
    });

    const targetIndex = firstRunning >= 0 ? firstRunning : sorted.length - 1;
    const targetItem = sorted[targetIndex];
    if (!targetItem) return;

    requestAnimationFrame(() => {
      const targetEl = document.getElementById(`stream-item-${targetItem.id}`);
      if (targetEl && scrollRef.current) {
        const containerRect = scrollRef.current.getBoundingClientRect();
        const elRect = targetEl.getBoundingClientRect();
        const offset = elRect.top - containerRect.top - containerRect.height / 3;
        scrollRef.current.scrollTop += offset;
      }
    });
  }, [viewModel, group]);

  useEffect(() => {
    if (!activeTextBlock) {
      return;
    }

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setActiveTextBlock(null);
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [activeTextBlock]);

  if (!group || !viewModel) {
    return (
      <div className="h-full min-w-0 overflow-y-auto overflow-x-hidden p-4">
        <p className="text-sm font-mono-ui opacity-40">Select a trace to inspect.</p>
      </div>
    );
  }

  return (
    <>
      <div ref={scrollRef} className="h-full min-w-0 space-y-3 overflow-y-auto overflow-x-hidden">
        <div className="p-4 pb-0">
          <TraceOverviewPanel viewModel={viewModel} />
        </div>

        <div className="px-4">
          <div className="flex items-center gap-2 border-b border-border/20 pb-1">
            <span className="text-[10px] font-mono-ui uppercase tracking-wider text-muted-foreground/60">
              Execution Stream
            </span>
            {group.activeStream && (
              <span className="relative flex h-2 w-2">
                <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75" />
                <span className="relative inline-flex rounded-full h-2 w-2 bg-emerald-500" />
              </span>
            )}
          </div>
        </div>

        <StreamTimeline
          viewModel={viewModel}
          onOpenFullscreen={setActiveTextBlock}
          traceId={group.traceId}
        />

        <div className="px-4 space-y-3 pb-4">
          {viewModel.rawContextBlocks.length > 0 && (
            <InspectorSection title="Context Debug" collapsible defaultExpanded={false}>
              <div className="space-y-3">
                {viewModel.rawContextBlocks.map((block) => (
                  <ExpandableTextPanel
                    key={block.label}
                    title="Captured Context"
                    label={block.label}
                    content={block.content}
                    visibility={getExpandableTextPanelVisibility({
                      title: "Captured Context",
                      label: block.label,
                      content: block.content,
                    })}
                    onOpenFullscreen={setActiveTextBlock}
                  />
                ))}
              </div>
            </InspectorSection>
          )}

          <RawEventsPanel
            events={group.rawEvents}
            controller={controller}
            showRawEvents={showRawEvents}
            onToggle={() => setShowRawEvents(!showRawEvents)}
          />
        </div>
      </div>

      <TextBlockOverlay
        block={activeTextBlock}
        onClose={() => setActiveTextBlock(null)}
      />
    </>
  );
}
