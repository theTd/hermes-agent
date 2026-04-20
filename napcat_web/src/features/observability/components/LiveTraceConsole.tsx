import { useEffect, useMemo, useRef } from "react";
import { TraceGroupCard } from "./TraceGroupCard";
import { pickLatestTraceGroup } from "../selectors";
import type { ObservabilityController } from "../types";

export function LiveTraceConsole({
  controller,
  sessionKey,
}: {
  controller: ObservabilityController;
  sessionKey: string | null;
}) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const visibleGroups = useMemo(
    () => controller.groups.filter((group) => !sessionKey || group.trace.session_key === sessionKey),
    [controller.groups, sessionKey],
  );
  const latestTraceId = useMemo(
    () => pickLatestTraceGroup(visibleGroups)?.traceId ?? pickLatestTraceGroup(controller.groups)?.traceId,
    [controller.groups, visibleGroups],
  );
  const selectedTraceId = controller.selectedGroup?.traceId ?? controller.state.ui.selectedTraceId;
  const followLatest = controller.state.connection.followLatest;
  const previousLatestTraceIdRef = useRef<string | null>(null);

  useEffect(() => {
    if (!followLatest || !latestTraceId) {
      previousLatestTraceIdRef.current = latestTraceId ?? null;
      return;
    }
    const latestChanged = previousLatestTraceIdRef.current !== latestTraceId;
    previousLatestTraceIdRef.current = latestTraceId;

    if (selectedTraceId !== latestTraceId) {
      controller.setSelectedTrace(latestTraceId);
    }
    if (latestChanged) {
      containerRef.current?.scrollTo({ top: 0, behavior: "smooth" });
    }
  }, [controller, followLatest, latestTraceId, selectedTraceId]);

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="border-b border-border/20 px-3 py-1.5">
        <div className="text-[10px] font-mono-ui uppercase tracking-wider opacity-50">
          Request List
        </div>
        <div className="text-xs font-mono-ui opacity-60">
          {visibleGroups.length} trace{visibleGroups.length === 1 ? "" : "s"} in session
        </div>
      </div>
      <div ref={containerRef} className="min-h-0 flex-1 overflow-y-auto">
        <div className="divide-y divide-border/10">
          {visibleGroups.length === 0 ? (
            <p className="px-3 py-2 text-xs font-mono-ui opacity-40">
              No traces for the selected session and current filters.
            </p>
          ) : (
            visibleGroups.map((group) => (
              <TraceGroupCard
                key={group.traceId}
                group={group}
                selected={controller.selectedGroup?.traceId === group.traceId}
                onSelect={() => controller.setSelectedTrace(group.traceId)}
              />
            ))
          )}
        </div>
      </div>
    </div>
  );
}
