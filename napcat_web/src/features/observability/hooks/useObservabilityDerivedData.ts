import { useMemo } from "react";
import type { ObservabilityState } from "../types";
import {
  selectEventRate10s,
  selectLiveTraceGroups,
  selectVisibleAlerts,
  selectVisibleSessions,
} from "../selectors";

export function useObservabilityDerivedData(state: ObservabilityState) {
  const groups = useMemo(() => selectLiveTraceGroups(state), [state]);
  const traces = useMemo(() => groups.map((group) => group.trace), [groups]);
  const sessions = useMemo(() => selectVisibleSessions(state), [state]);
  const alerts = useMemo(() => selectVisibleAlerts(state), [state]);
  const selectedGroup = useMemo(() => {
    const selectedTraceId = state.ui.selectedTraceId;
    if (selectedTraceId) {
      return groups.find((group) => group.traceId === selectedTraceId) ?? groups[0] ?? null;
    }
    return groups[0] ?? null;
  }, [groups, state.ui.selectedTraceId]);
  const selectedTrace = useMemo(
    () => selectedGroup?.trace ?? null,
    [selectedGroup],
  );
  const selectedTraceEvents = useMemo(
    () => selectedGroup?.rawEvents ?? [],
    [selectedGroup],
  );
  const eventRate10s = useMemo(() => selectEventRate10s(state), [state]);

  return {
    groups,
    traces,
    sessions,
    alerts,
    selectedGroup,
    selectedTrace,
    selectedTraceEvents,
    eventRate10s,
  };
}
