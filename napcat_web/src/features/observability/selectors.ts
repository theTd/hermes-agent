import type { NapcatAlert, NapcatEvent, NapcatSession, NapcatTrace } from "@/lib/napcat-api";
import { mapTraceToLiveGroup } from "./stage-mapper";
import { createDefaultFilters } from "./types";
import type {
  FilterState,
  LiveTraceGroup,
  ObservabilityState,
  ObservabilityView,
  SessionTimelineGroup,
} from "./types";

const VIEW_PRESETS: Record<ObservabilityView, Partial<FilterState>> = {
  all: { view: "all", status: "", eventType: "" },
  active: { view: "active", status: "in_progress", eventType: "" },
  errors: { view: "errors", status: "error", eventType: "" },
  rejected: { view: "rejected", status: "rejected", eventType: "" },
  tools: { view: "tools", status: "", eventType: "" },
  streaming: { view: "streaming", status: "", eventType: "" },
  alerts: { view: "alerts", status: "error", eventType: "" },
};

function includesSearch(haystack: string | null | undefined, needle: string): boolean {
  if (!haystack) {
    return false;
  }
  return haystack.toLowerCase().includes(needle);
}

function traceMatchesSearch(group: LiveTraceGroup, search: string): boolean {
  if (!search) {
    return true;
  }
  if (
    [
      group.trace.trace_id,
      group.trace.session_key,
      group.trace.session_id,
      group.trace.chat_id,
      group.trace.user_id,
      group.trace.message_id,
      group.status,
      group.model,
      group.headline,
      group.mainOrchestrator?.summary,
      group.mainOrchestrator?.reasoningSummary,
      ...group.childTasks.flatMap((child) => [
        child.childId,
        child.goal,
        child.summary,
        child.error,
        child.reason,
        child.runtimeSessionId,
        child.runtimeSessionKey,
      ]),
      ...Object.values(group.childDetailsById).flatMap((child) => [child.model]),
    ].some((value) => includesSearch(value, search))
  ) {
    return true;
  }

  try {
    if (includesSearch(JSON.stringify(group.trace.summary), search)) {
      return true;
    }
  } catch {
    // Ignore summary serialization failures.
  }

  return group.rawEvents.some((event) => {
    try {
      return includesSearch(event.event_type, search) || includesSearch(JSON.stringify(event.payload), search);
    } catch {
      return includesSearch(event.event_type, search);
    }
  });
}

function isSystemLifecycleGroup(group: LiveTraceGroup): boolean {
  const summaryEventTypes = Array.isArray(group.trace.summary?.event_types)
    ? group.trace.summary.event_types.map((eventType) => String(eventType))
    : [];
  if (group.rawEvents.length === 0 && summaryEventTypes.length === 0) {
    return false;
  }
  const eventTypes = group.rawEvents.length > 0
    ? group.rawEvents.map((event) => event.event_type)
    : summaryEventTypes;
  return (
    eventTypes.every((eventType) => eventType.startsWith("adapter."))
    && !group.trace.chat_id
    && !group.trace.message_id
    && group.headlineSource === "empty"
    && group.toolCallCount === 0
  );
}

function groupMatchesFilters(group: LiveTraceGroup, state: ObservabilityState): boolean {
  if (isSystemLifecycleGroup(group)) {
    return false;
  }
  const { filters } = state;
  if (filters.status && group.status !== filters.status) {
    return false;
  }
  if (filters.sessionKey && group.trace.session_key !== filters.sessionKey) {
    return false;
  }
  if (filters.chatId && group.trace.chat_id !== filters.chatId) {
    return false;
  }
  if (filters.userId && group.trace.user_id !== filters.userId) {
    return false;
  }
  const summaryEventTypes = Array.isArray(group.trace.summary?.event_types)
    ? group.trace.summary.event_types.map((eventType) => String(eventType))
    : [];
  if (
    filters.eventType
    && !group.rawEvents.some((event) => event.event_type === filters.eventType)
    && !summaryEventTypes.includes(filters.eventType)
  ) {
    return false;
  }
  if (filters.view === "tools" && group.toolCallCount === 0) {
    return false;
  }
  if (filters.view === "streaming" && !group.activeStream) {
    return false;
  }
  if (filters.view === "alerts" && group.errorCount === 0) {
    return false;
  }

  return traceMatchesSearch(group, filters.search.trim().toLowerCase());
}

function sortGroups(groups: LiveTraceGroup[]): LiveTraceGroup[] {
  return [...groups].sort((left, right) => {
    if (left.startedAt !== right.startedAt) {
      return right.startedAt - left.startedAt;
    }
    if (left.lastEventAt !== right.lastEventAt) {
      return right.lastEventAt - left.lastEventAt;
    }
    return left.traceId.localeCompare(right.traceId);
  });
}

export function pickLatestTraceGroup(groups: LiveTraceGroup[]): LiveTraceGroup | null {
  let latest: LiveTraceGroup | null = null;
  for (const group of groups) {
    if (!latest) {
      latest = group;
      continue;
    }
    if (group.startedAt !== latest.startedAt) {
      if (group.startedAt > latest.startedAt) {
        latest = group;
      }
      continue;
    }
    if (group.lastEventAt !== latest.lastEventAt) {
      if (group.lastEventAt > latest.lastEventAt) {
        latest = group;
      }
      continue;
    }
    if (group.traceId.localeCompare(latest.traceId) > 0) {
      latest = group;
    }
  }
  return latest;
}

function sortSessions(sessions: NapcatSession[]): NapcatSession[] {
  return [...sessions].sort((left, right) => {
    if (left.last_seen !== right.last_seen) {
      return right.last_seen - left.last_seen;
    }
    return left.session_key.localeCompare(right.session_key);
  });
}

function sortAlerts(alerts: NapcatAlert[]): NapcatAlert[] {
  return [...alerts].sort((left, right) => {
    if (left.ts !== right.ts) {
      return right.ts - left.ts;
    }
    return left.alert_id.localeCompare(right.alert_id);
  });
}

export function presetFiltersForView(view: ObservabilityView): FilterState {
  return {
    ...createDefaultFilters(),
    ...VIEW_PRESETS[view],
  };
}

export function selectTraceEvents(
  state: ObservabilityState,
  traceId: string | null | undefined,
): NapcatEvent[] {
  if (!traceId) {
    return [];
  }
  return state.entities.eventsByTraceId[traceId] ?? [];
}

export function selectLiveTraceGroups(state: ObservabilityState): LiveTraceGroup[] {
  const groups = Object.values(state.entities.tracesById).map((trace) => {
    const backendGroup = state.entities.groupsById[trace.trace_id];
    const events = state.entities.eventsByTraceId[trace.trace_id] ?? [];
    // Prefer the backend-computed view model when it is present and its
    // rawEvents are at least as complete as the frontend event cache.
    // This avoids losing waterfall / advanced data when the frontend
    // event cache has been pruned or never populated (e.g. after refresh).
    if (
      backendGroup
      && (backendGroup.rawEvents?.length ?? 0) >= events.length
      && backendGroup.timeline?.lanes?.length > 0
    ) {
      return backendGroup;
    }
    return mapTraceToLiveGroup(trace, events);
  });
  return sortGroups(groups.filter((group) => groupMatchesFilters(group, state)));
}

export function selectVisibleTraces(state: ObservabilityState): NapcatTrace[] {
  return selectLiveTraceGroups(state).map((group) => group.trace);
}

export function selectSelectedTrace(state: ObservabilityState): NapcatTrace | null {
  const selectedTraceId = state.ui.selectedTraceId;
  if (selectedTraceId && state.entities.tracesById[selectedTraceId]) {
    return state.entities.tracesById[selectedTraceId];
  }
  return selectVisibleTraces(state)[0] ?? null;
}

export function selectSelectedTraceEvents(state: ObservabilityState): NapcatEvent[] {
  return selectTraceEvents(state, selectSelectedTrace(state)?.trace_id);
}

export function selectSelectedGroup(state: ObservabilityState): LiveTraceGroup | null {
  const selectedTraceId = state.ui.selectedTraceId;
  const groups = selectLiveTraceGroups(state);
  if (selectedTraceId) {
    return groups.find((group) => group.traceId === selectedTraceId) ?? null;
  }
  return groups[0] ?? null;
}

export function selectVisibleSessions(state: ObservabilityState): NapcatSession[] {
  return sortSessions(Object.values(state.entities.sessionsByKey));
}

export function selectSessionTimelineGroups(state: ObservabilityState): SessionTimelineGroup[] {
  const groups = selectLiveTraceGroups(state);
  const sessionsByKey = new Map<string, SessionTimelineGroup>();

  for (const group of groups) {
    if (!group.trace.session_key) {
      continue;
    }
    const existing = sessionsByKey.get(group.trace.session_key);
    if (existing) {
      existing.traces.push(group);
      existing.lastSeen = Math.max(existing.lastSeen, group.lastEventAt);
      if (group.status === "in_progress") {
        existing.activeCount += 1;
      }
      if (group.status === "error") {
        existing.errorCount += 1;
      }
      continue;
    }
    sessionsByKey.set(group.trace.session_key, {
      sessionKey: group.trace.session_key,
      session: state.entities.sessionsByKey[group.trace.session_key] ?? null,
      traces: [group],
      lastSeen: group.lastEventAt,
      activeCount: group.status === "in_progress" ? 1 : 0,
      errorCount: group.status === "error" ? 1 : 0,
    });
  }

  return [...sessionsByKey.values()].sort((left, right) => {
    if (left.lastSeen !== right.lastSeen) {
      return right.lastSeen - left.lastSeen;
    }
    return left.sessionKey.localeCompare(right.sessionKey);
  });
}

export function selectVisibleAlerts(state: ObservabilityState): NapcatAlert[] {
  return sortAlerts(
    Object.values(state.entities.alertsById).filter((alert) => {
      if (state.filters.view === "alerts") {
        return true;
      }
      if (state.ui.selectedTraceId) {
        return alert.trace_id === state.ui.selectedTraceId;
      }
      return true;
    }),
  );
}

export function selectEventRate10s(state: ObservabilityState): number {
  const latest = state.connection.latestCursor;
  if (latest == null) {
    return 0;
  }

  let count = 0;
  for (const events of Object.values(state.entities.eventsByTraceId)) {
    for (const event of events) {
      if (latest - event.ts <= 10) {
        count += 1;
      }
    }
  }
  return count;
}
