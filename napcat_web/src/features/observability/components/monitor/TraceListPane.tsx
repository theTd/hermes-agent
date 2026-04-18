import { TraceGroupCard } from "../TraceGroupCard";
import type { TraceListItemViewModel } from "../../domain/monitor-view";

export function TraceListPane({
  items,
  onSelectTrace,
}: {
  items: TraceListItemViewModel[];
  onSelectTrace: (traceId: string) => void;
}) {
  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="border-b border-border/20 px-4 py-3">
        <div className="text-[10px] font-compressed uppercase tracking-[0.3em] opacity-50">
          Request List
        </div>
        <div className="mt-2 text-xs font-mono-ui opacity-60">
          {items.length} trace{items.length === 1 ? "" : "s"} in session
        </div>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto p-4">
        <div className="space-y-3">
          {items.length === 0 ? (
            <p className="text-sm font-mono-ui opacity-40">
              No traces for the selected session and current filters.
            </p>
          ) : (
            items.map((item) => (
              <TraceGroupCard
                key={item.group.traceId}
                group={item.group}
                selected={item.selected}
                lanePreviews={item.lanePreviews}
                onSelect={() => onSelectTrace(item.group.traceId)}
              />
            ))
          )}
        </div>
      </div>
    </div>
  );
}
