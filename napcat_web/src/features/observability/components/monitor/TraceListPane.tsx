import { useRef, useEffect } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import { TraceGroupCard } from "../TraceGroupCard";
import type { TraceListItemViewModel } from "../../domain/monitor-view";

interface TraceListPaneProps {
  items: TraceListItemViewModel[];
  selectedTraceId: string | null;
  onSelectTrace: (traceId: string) => void;
}

export function TraceListPane({
  items,
  selectedTraceId,
  onSelectTrace,
}: TraceListPaneProps) {
  const parentRef = useRef<HTMLDivElement>(null);

  const virtualizer = useVirtualizer({
    count: items.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => 36,
    overscan: 8,
    getItemKey: (index) => items[index]?.group.traceId ?? index,
  });

  const virtualItems = virtualizer.getVirtualItems();

  // Scroll selected trace into view.
  useEffect(() => {
    if (!selectedTraceId) return;
    const index = items.findIndex((item) => item.group.traceId === selectedTraceId);
    if (index >= 0) {
      virtualizer.scrollToIndex(index, { align: "auto", behavior: "smooth" });
    }
  }, [selectedTraceId, items, virtualizer]);

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex items-center gap-2 border-b border-border/20 px-3 py-1.5 text-[10px] font-mono-ui uppercase tracking-wider opacity-50">
        <div className="h-2 w-2 shrink-0" />
        <span className="w-16 shrink-0">Time</span>
        <span className="w-12 shrink-0">Dur</span>
        <span className="min-w-0 flex-1">Headline</span>
        <span className="w-8 shrink-0 text-right">Evt</span>
        <span className="w-8 shrink-0 text-right">LLM</span>
        <span className="w-6 shrink-0 text-right">Err</span>
      </div>
      <div ref={parentRef} className="min-h-0 flex-1 overflow-y-auto">
        {items.length === 0 ? (
          <p className="px-3 py-2 text-xs font-mono-ui opacity-40">
            No traces for the selected session and current filters.
          </p>
        ) : (
          <div
            className="relative w-full"
            style={{ height: `${virtualizer.getTotalSize()}px` }}
          >
            {virtualItems.map((virtualItem) => {
              const item = items[virtualItem.index];
              if (!item) return null;
              return (
                <div
                  key={virtualItem.key}
                  ref={virtualizer.measureElement}
                  data-index={virtualItem.index}
                  className="absolute left-0 top-0 w-full"
                  style={{
                    transform: `translateY(${virtualItem.start}px)`,
                  }}
                >
                  <TraceGroupCard
                    group={item.group}
                    selected={item.selected}
                    lanePreviews={item.lanePreviews}
                    onSelect={() => onSelectTrace(item.group.traceId)}
                  />
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
