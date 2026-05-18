import type { NapcatEvent, NapcatSession, NapcatTrace } from "@/lib/napcat-api";
import { createDefaultFilters } from "./types";
import type { BootstrapSnapshot, ObservabilityAction, ObservabilityState } from "./types";

const MAX_TRACE_COUNT = 200;
const MAX_EVENTS_PER_TRACE = 500;
const MAX_ALERT_COUNT = 100;
const MAX_SESSION_COUNT = 100;

function createEmptyEntities(): ObservabilityState["entities"] {
  return {
    tracesById: {},
    eventsByTraceId: {},
    alertsById: {},
    sessionsByKey: {},
    groupsById: {},
  };
}

export function createInitialObservabilityState(): ObservabilityState {
  return {
    connection: {
      wsConnected: false,
      paused: false,
      followLatest: false,
      reconnecting: false,
      latestCursor: null,
      unreadWhilePaused: 0,
    },
    filters: {
      ...createDefaultFilters(),
    },
    entities: createEmptyEntities(),
    runtime: {},
    stats: null,
    dashboardStats: null,
    ui: {
      selectedTraceId: null,
      expandedTraceIds: {},
      expandedRawEventIds: {},
      activeInspectorSection: null,
    },
    bootstrapped: false,
    error: null,
  };
}

function mergeEvents(existing: NapcatEvent[], incoming: NapcatEvent): NapcatEvent[] {
  if (existing.some((event) => event.event_id === incoming.event_id)) {
    return existing;
  }

  return [...existing, incoming].sort((left, right) => {
    if (left.ts !== right.ts) {
      return left.ts - right.ts;
    }
    return left.event_id.localeCompare(right.event_id);
  });
}

function normalizeSessions(
  sessions: NapcatSession[] | undefined,
  traces: NapcatTrace[] | undefined,
): Record<string, NapcatSession> {
  const sessionsByKey: Record<string, NapcatSession> = {};

  for (const session of sessions ?? []) {
    sessionsByKey[session.session_key] = session;
  }

  for (const trace of traces ?? []) {
    if (sessionsByKey[trace.session_key]) {
      continue;
    }
    const next = mergeTraceIntoSession(sessionsByKey[trace.session_key], trace, undefined);
    if (next) {
      sessionsByKey[next.session_key] = next;
    }
  }

  return sessionsByKey;
}

function normalizeEvents(traces: NapcatTrace[] | undefined): Record<string, NapcatEvent[]> {
  const eventsByTraceId: Record<string, NapcatEvent[]> = {};
  for (const trace of traces ?? []) {
    if (trace.events?.length) {
      eventsByTraceId[trace.trace_id] = [...trace.events].sort((left, right) => {
        if (left.ts !== right.ts) {
          return left.ts - right.ts;
        }
        return left.event_id.localeCompare(right.event_id);
      });
    }
  }
  return eventsByTraceId;
}

function normalizeEventList(events: NapcatEvent[] | undefined): Record<string, NapcatEvent[]> {
  const eventsByTraceId: Record<string, NapcatEvent[]> = {};
  for (const event of events ?? []) {
    eventsByTraceId[event.trace_id] = mergeEvents(eventsByTraceId[event.trace_id] ?? [], event);
  }
  return eventsByTraceId;
}

function mergeEventMaps(
  existing: Record<string, NapcatEvent[]>,
  incoming: Record<string, NapcatEvent[]>,
): Record<string, NapcatEvent[]> {
  const eventsByTraceId = { ...existing };
  for (const [traceId, events] of Object.entries(incoming)) {
    let merged = eventsByTraceId[traceId] ?? [];
    for (const event of events) {
      merged = mergeEvents(merged, event);
    }
    eventsByTraceId[traceId] = trimEvents(merged);
  }
  return eventsByTraceId;
}

function trimEvents(events: NapcatEvent[]): NapcatEvent[] {
  return events.length > MAX_EVENTS_PER_TRACE ? events.slice(-MAX_EVENTS_PER_TRACE) : events;
}

function shouldUseFastNewTracePath(
  state: ObservabilityState,
  event: NapcatEvent,
  sessionKey: string,
): boolean {
  if (event.event_type !== "trace.created") {
    return false;
  }
  if (state.entities.tracesById[event.trace_id]) {
    return false;
  }
  if (Object.keys(state.entities.tracesById).length >= MAX_TRACE_COUNT) {
    return false;
  }
  if (
    sessionKey
    && !state.entities.sessionsByKey[sessionKey]
    && Object.keys(state.entities.sessionsByKey).length >= MAX_SESSION_COUNT
  ) {
    return false;
  }
  return true;
}

function normalizeTraces(traces: NapcatTrace[] | undefined): Record<string, NapcatTrace> {
  const tracesById: Record<string, NapcatTrace> = {};
  for (const trace of traces ?? []) {
    tracesById[trace.trace_id] = trace;
  }
  return tracesById;
}

function normalizeAlerts(
  alerts: BootstrapSnapshot["alerts"],
): ObservabilityState["entities"]["alertsById"] {
  const alertsById: ObservabilityState["entities"]["alertsById"] = {};
  for (const alert of alerts ?? []) {
    alertsById[alert.alert_id] = alert;
  }
  return alertsById;
}

function pruneEntities(
  entities: ObservabilityState["entities"],
  selectedTraceId: string | null,
): ObservabilityState["entities"] {
  const traceEntries = Object.entries(entities.tracesById);
  const sortedTraceIds = traceEntries
    .sort(([, left], [, right]) => {
      const leftEvents = entities.eventsByTraceId[left.trace_id] ?? [];
      const rightEvents = entities.eventsByTraceId[right.trace_id] ?? [];
      const leftTs = leftEvents[leftEvents.length - 1]?.ts ?? left.ended_at ?? left.started_at;
      const rightTs = rightEvents[rightEvents.length - 1]?.ts ?? right.ended_at ?? right.started_at;
      return rightTs - leftTs;
    })
    .map(([traceId]) => traceId);

  const keptTraceIds = new Set(sortedTraceIds.slice(0, MAX_TRACE_COUNT));
  if (selectedTraceId) {
    keptTraceIds.add(selectedTraceId);
  }

  const tracesById: ObservabilityState["entities"]["tracesById"] = {};
  const eventsByTraceId: ObservabilityState["entities"]["eventsByTraceId"] = {};
  for (const traceId of keptTraceIds) {
    const trace = entities.tracesById[traceId];
    if (!trace) {
      continue;
    }
    tracesById[traceId] = trace;
    const events = entities.eventsByTraceId[traceId];
    if (events?.length) {
      eventsByTraceId[traceId] = trimEvents(events);
    }
  }

  const alertsById = Object.fromEntries(
    Object.entries(entities.alertsById)
      .sort(([, left], [, right]) => right.ts - left.ts)
      .slice(0, MAX_ALERT_COUNT),
  );

  const sessionsByKey = Object.fromEntries(
    Object.entries(entities.sessionsByKey)
      .sort(([, left], [, right]) => right.last_seen - left.last_seen)
      .slice(0, MAX_SESSION_COUNT),
  );

  const groupsById: ObservabilityState["entities"]["groupsById"] = {};
  for (const traceId of keptTraceIds) {
    if (entities.groupsById[traceId]) {
      groupsById[traceId] = entities.groupsById[traceId];
    }
  }

  return {
    tracesById,
    eventsByTraceId,
    alertsById,
    sessionsByKey,
    groupsById,
  };
}

function mergeSessionsWithTraces(
  existingSessions: Record<string, NapcatSession>,
  traces: NapcatTrace[] | undefined,
  tracesById: Record<string, NapcatTrace>,
): Record<string, NapcatSession> {
  const sessionsByKey = { ...existingSessions };
  for (const trace of traces ?? []) {
    const previousTrace = tracesById[trace.trace_id];
    const nextSession = mergeTraceIntoSession(
      sessionsByKey[trace.session_key],
      trace,
      previousTrace,
    );
    if (nextSession) {
      sessionsByKey[nextSession.session_key] = nextSession;
    }
  }
  return sessionsByKey;
}

function mergeTraceIntoSession(
  existing: NapcatSession | undefined,
  trace: NapcatTrace,
  previousTrace: NapcatTrace | undefined,
): NapcatSession | null {
  if (!trace.session_key) {
    return null;
  }

  const base: NapcatSession = existing ?? {
    session_key: trace.session_key,
    session_id: trace.session_id,
    trace_count: 0,
    first_seen: trace.started_at,
    last_seen: trace.started_at,
    total_events: 0,
  };

  const previousEventCount = previousTrace?.event_count ?? 0;
  const previousStartedAt = previousTrace?.started_at ?? trace.started_at;
  const isNewTrace = previousTrace == null;

  return {
    session_key: trace.session_key,
    session_id: trace.session_id || base.session_id,
    trace_count: base.trace_count + (isNewTrace ? 1 : 0),
    first_seen: Math.min(base.first_seen, trace.started_at),
    last_seen: Math.max(base.last_seen, trace.started_at, previousStartedAt),
    total_events: Math.max(0, base.total_events - previousEventCount + trace.event_count),
  };
}

const TRACE_TERMINAL_EVENT_TYPES = new Set([
  "agent.response.final",
  "agent.response.suppressed",
  "gateway.turn.short_circuited",
  "orchestrator.status.reply",
  "orchestrator.turn.completed",
  "orchestrator.turn.failed",
  "orchestrator.turn.ignored",
  "orchestrator.child.completed",
  "orchestrator.child.cancelled",
  "orchestrator.child.failed",
  "error.raised",
  "group.trigger.rejected",
]);

const TRACE_MAIN_COMPLETED_EVENT_TYPES = new Set([
  "agent.response.final",
  "agent.response.suppressed",
  "gateway.turn.short_circuited",
  "orchestrator.status.reply",
  "orchestrator.turn.completed",
]);

const TRACE_MAIN_IGNORED_EVENT_TYPES = new Set([
  "orchestrator.turn.ignored",
]);

const TRACE_MAIN_ERROR_EVENT_TYPES = new Set([
  "error.raised",
  "orchestrator.turn.failed",
]);

function analyzeTraceLifecycle(events: NapcatEvent[]): {
  status: string;
  endedAt: number | null;
} {
  if (events.length === 0) {
    return {
      status: "in_progress",
      endedAt: null,
    };
  }

  const childStatuses = new Map<string, string>();
  let mainOutcome = "";
  let rejectedAt: number | null = null;
  let lastTerminalTs: number | null = null;

  for (const event of events) {
    const childId = String(event.payload?.child_id || "").trim();
    if (event.event_type === "orchestrator.child.spawned") {
      if (childId && !childStatuses.has(childId)) {
        childStatuses.set(childId, "active");
      }
    } else if (event.event_type === "orchestrator.child.completed") {
      if (childId) {
        childStatuses.set(childId, "completed");
      }
    } else if (event.event_type === "orchestrator.child.cancelled") {
      if (childId) {
        childStatuses.set(childId, "cancelled");
      }
    } else if (event.event_type === "orchestrator.child.failed") {
      if (childId) {
        childStatuses.set(childId, "failed");
      }
    }

    if (event.event_type === "group.trigger.rejected" && rejectedAt == null) {
      rejectedAt = event.ts;
    }

    if (TRACE_MAIN_COMPLETED_EVENT_TYPES.has(event.event_type)) {
      mainOutcome = "completed";
    } else if (TRACE_MAIN_IGNORED_EVENT_TYPES.has(event.event_type)) {
      mainOutcome = "ignored";
    } else if (TRACE_MAIN_ERROR_EVENT_TYPES.has(event.event_type)) {
      mainOutcome = "error";
    }

    if (TRACE_TERMINAL_EVENT_TYPES.has(event.event_type)) {
      lastTerminalTs = Math.max(lastTerminalTs ?? event.ts, event.ts);
    }
  }

  const childStates = [...childStatuses.values()];
  const hasActiveChild = childStates.includes("active");
  const hasFailedChild = childStates.includes("failed");
  const hasChildWork = childStates.length > 0;

  let status = "in_progress";
  if (hasActiveChild) {
    status = "in_progress";
  } else if (mainOutcome === "error" || hasFailedChild) {
    status = "error";
  } else if (mainOutcome === "ignored") {
    status = "ignored";
  } else if (mainOutcome === "completed") {
    status = "completed";
  } else if (hasChildWork) {
    status = "completed";
  } else if (rejectedAt != null) {
    status = "rejected";
  }

  return {
    status,
    endedAt: status === "in_progress" ? null : lastTerminalTs,
  };
}

function reconcileTraceLifecycle(
  trace: NapcatTrace,
  events: NapcatEvent[] | undefined,
): NapcatTrace {
  if (!events?.length) {
    return trace;
  }

  const lifecycle = analyzeTraceLifecycle(events);
  const nextStatus = lifecycle.status === "in_progress" && trace.status !== "in_progress"
    ? trace.status
    : lifecycle.status;
  const nextEndedAt = nextStatus === "in_progress"
    ? null
    : lifecycle.endedAt ?? trace.ended_at;

  return {
    ...trace,
    events,
    event_count: Math.max(trace.event_count, events.length),
    ended_at: nextEndedAt,
    status: nextStatus,
  };
}

function pickFollowLatestTraceId(
  tracesById: Record<string, NapcatTrace>,
): string | null {
  let latestTrace: NapcatTrace | null = null;
  for (const trace of Object.values(tracesById)) {
    if (!latestTrace) {
      latestTrace = trace;
      continue;
    }
    if (trace.started_at !== latestTrace.started_at) {
      if (trace.started_at > latestTrace.started_at) {
        latestTrace = trace;
      }
      continue;
    }
    if ((trace.ended_at ?? 0) !== (latestTrace.ended_at ?? 0)) {
      if ((trace.ended_at ?? 0) > (latestTrace.ended_at ?? 0)) {
        latestTrace = trace;
      }
      continue;
    }
    if (trace.trace_id.localeCompare(latestTrace.trace_id) > 0) {
      latestTrace = trace;
    }
  }
  return latestTrace?.trace_id ?? null;
}

function mergeEventIntoTrace(
  trace: NapcatTrace | undefined,
  events: NapcatEvent[],
  event: NapcatEvent,
): NapcatTrace {
  const current: NapcatTrace = trace ?? {
    trace_id: event.trace_id,
    session_key: event.session_key,
    session_id: event.session_id,
    chat_type: event.chat_type,
    chat_id: event.chat_id,
    user_id: event.user_id,
    message_id: event.message_id,
    started_at: event.ts,
    ended_at: null,
    event_count: 0,
    status: "in_progress",
    summary: {},
  };

  const nextEvents = mergeEvents(events, event);
  if (nextEvents.length === events.length) {
    return current;
  }

  const lifecycle = analyzeTraceLifecycle(nextEvents);

  return {
    ...current,
    session_key: current.session_key || event.session_key,
    session_id: current.session_id || event.session_id,
    chat_type: current.chat_type || event.chat_type,
    chat_id: current.chat_id || event.chat_id,
    user_id: current.user_id || event.user_id,
    message_id: current.message_id || event.message_id,
    events: nextEvents,
    event_count: Math.max(current.event_count, nextEvents.length),
    ended_at: lifecycle.endedAt,
    status: lifecycle.status,
  };
}

function hasPartialChange<T extends object>(
  current: T,
  patch: Partial<T>,
): boolean {
  return Object.entries(patch).some(([key, value]) => {
    const typedKey = key as keyof T;
    return current[typedKey] !== value;
  });
}

export function observabilityReducer(
  state: ObservabilityState,
  action: ObservabilityAction,
): ObservabilityState {
  switch (action.type) {
    case "bootstrap_from_snapshot": {
      const replaceEntities = action.payload.replaceEntities === true;

      // If backend view models (groups) are provided, extract raw traces and events from them.
      const tracesFromGroups = action.payload.groups?.map((g) => g.trace) ?? [];
      const eventsFromGroups = action.payload.groups?.flatMap((g) => g.rawEvents) ?? [];
      const payloadTraces = action.payload.traces
        ? [...tracesFromGroups, ...action.payload.traces]
        : tracesFromGroups;
      const payloadEvents = action.payload.events
        ? [...eventsFromGroups, ...action.payload.events]
        : eventsFromGroups;

      const baseTraces = replaceEntities ? {} : state.entities.tracesById;
      const initialMergedTraces = payloadTraces.length > 0
        ? { ...baseTraces, ...normalizeTraces(payloadTraces) }
        : baseTraces;
      const traceEvents = normalizeEvents(payloadTraces);
      const payloadEventsList = normalizeEventList(payloadEvents);
      const baseEvents = replaceEntities
        ? Object.fromEntries(
            Object.entries(state.entities.eventsByTraceId).filter(([traceId]) => initialMergedTraces[traceId]),
          )
        : state.entities.eventsByTraceId;
      const mergedEvents = mergeEventMaps(
        baseEvents,
        mergeEventMaps(traceEvents, payloadEventsList),
      );
      const mergedTraces = Object.fromEntries(
        Object.entries(initialMergedTraces).map(([traceId, trace]) => [
          traceId,
          reconcileTraceLifecycle(trace, mergedEvents[traceId]),
        ]),
      );
      const baseAlerts = replaceEntities ? {} : state.entities.alertsById;
      const mergedAlerts = action.payload.alerts
        ? { ...baseAlerts, ...normalizeAlerts(action.payload.alerts) }
        : baseAlerts;
      const mergedSessions = action.payload.sessions
        ? (
            replaceEntities
              ? normalizeSessions(action.payload.sessions, payloadTraces)
              : {
                  ...state.entities.sessionsByKey,
                  ...normalizeSessions(action.payload.sessions, payloadTraces),
                }
          )
        : mergeSessionsWithTraces(
            replaceEntities ? {} : state.entities.sessionsByKey,
            payloadTraces,
            replaceEntities ? {} : state.entities.tracesById,
          );

      // Merge backend-computed view models
      const payloadGroups = action.payload.groups
        ? Object.fromEntries(action.payload.groups.map((g) => [g.traceId, g]))
        : {};
      const baseGroups = replaceEntities ? {} : state.entities.groupsById;
      const mergedGroups = { ...baseGroups, ...payloadGroups };
      // Preserve more complete hydrated groups over sparser bootstrap groups
      for (const [traceId, stateGroup] of Object.entries(state.entities.groupsById)) {
        const payloadGroup = payloadGroups[traceId];
        if (!payloadGroup) continue;
        if ((stateGroup.rawEvents?.length ?? 0) > (payloadGroup.rawEvents?.length ?? 0)) {
          mergedGroups[traceId] = stateGroup;
        }
      }

      // When replaceEntities is true and the currently-selected trace is not in the
      // incoming payload, preserve its old data so the inspector does not go blank.
      if (replaceEntities && state.ui.selectedTraceId && !mergedTraces[state.ui.selectedTraceId]) {
        const selectedId = state.ui.selectedTraceId;
        const oldTrace = state.entities.tracesById[selectedId];
        const oldEvents = state.entities.eventsByTraceId[selectedId];
        const oldGroup = state.entities.groupsById[selectedId];
        if (oldTrace) {
          mergedTraces[selectedId] = oldTrace;
        }
        if (oldEvents?.length) {
          mergedEvents[selectedId] = oldEvents;
        }
        if (oldGroup) {
          mergedGroups[selectedId] = oldGroup;
        }
      }

      const selectedTraceId = state.connection.followLatest
        ? pickFollowLatestTraceId(mergedTraces)
        : state.ui.selectedTraceId && mergedTraces[state.ui.selectedTraceId]
          ? state.ui.selectedTraceId
          : state.ui.selectedTraceId ?? payloadTraces[0]?.trace_id ?? null;
      const entities = pruneEntities({
        tracesById: mergedTraces,
        eventsByTraceId: mergedEvents,
        alertsById: mergedAlerts,
        sessionsByKey: mergedSessions,
        groupsById: mergedGroups,
      }, selectedTraceId);

      return {
        ...state,
        connection: {
          ...state.connection,
          latestCursor: action.payload.cursor ?? state.connection.latestCursor,
        },
        entities: {
          tracesById: entities.tracesById,
          eventsByTraceId: entities.eventsByTraceId,
          alertsById: entities.alertsById,
          sessionsByKey: entities.sessionsByKey,
          groupsById: entities.groupsById,
        },
        runtime: action.payload.runtime ?? state.runtime,
        stats: action.payload.stats ?? state.stats,
        dashboardStats: action.payload.dashboardStats ?? state.dashboardStats,
        ui: {
          ...state.ui,
          selectedTraceId,
        },
        bootstrapped: true,
        error: null,
      };
    }
    case "apply_trace_update": {
      const previousTrace = state.entities.tracesById[action.payload.trace.trace_id];
      const nextTrace = reconcileTraceLifecycle({
        ...previousTrace,
        ...action.payload.trace,
      }, state.entities.eventsByTraceId[action.payload.trace.trace_id]);
      const nextSession = mergeTraceIntoSession(
        state.entities.sessionsByKey[nextTrace.session_key],
        nextTrace,
        previousTrace,
      );
      const nextTracesById = {
        ...state.entities.tracesById,
        [nextTrace.trace_id]: nextTrace,
      };
      const selectedTraceId = state.connection.followLatest
        ? pickFollowLatestTraceId(nextTracesById)
        : state.ui.selectedTraceId;

      // Invalidate backend-computed view model for this trace so it gets recomputed frontend-side.
      const nextGroupsById = { ...state.entities.groupsById };
      delete nextGroupsById[action.payload.trace.trace_id];

      const entities = pruneEntities({
        ...state.entities,
        tracesById: nextTracesById,
        groupsById: nextGroupsById,
        sessionsByKey: nextSession
          ? {
              ...state.entities.sessionsByKey,
              [nextSession.session_key]: nextSession,
            }
          : state.entities.sessionsByKey,
      }, selectedTraceId);

      return {
        ...state,
        entities,
        ui: {
          ...state.ui,
          selectedTraceId,
        },
      };
    }
    case "apply_group_update": {
      const group = action.payload.group;
      const traceId = group.traceId;
      const trace = group.trace;
      const events = group.rawEvents ?? [];

      // Merge incoming events into the existing cache
      let mergedEvents = state.entities.eventsByTraceId[traceId] ?? [];
      for (const event of events) {
        mergedEvents = mergeEvents(mergedEvents, event);
      }

      const previousTrace = state.entities.tracesById[traceId];
      const nextTrace = reconcileTraceLifecycle(trace, mergedEvents);
      const nextSession = mergeTraceIntoSession(
        state.entities.sessionsByKey[nextTrace.session_key],
        nextTrace,
        previousTrace,
      );

      const nextGroupsById = { ...state.entities.groupsById, [traceId]: group };
      const nextTracesById = { ...state.entities.tracesById, [traceId]: nextTrace };
      const nextEventsByTraceId = { ...state.entities.eventsByTraceId, [traceId]: trimEvents(mergedEvents) };

      const entities = pruneEntities({
        ...state.entities,
        tracesById: nextTracesById,
        eventsByTraceId: nextEventsByTraceId,
        groupsById: nextGroupsById,
        sessionsByKey: nextSession
          ? { ...state.entities.sessionsByKey, [nextSession.session_key]: nextSession }
          : state.entities.sessionsByKey,
      }, state.ui.selectedTraceId);

      return {
        ...state,
        entities,
      };
    }
    case "append_event": {
      const currentEvents = state.entities.eventsByTraceId[action.payload.event.trace_id] ?? [];
      const nextEvents = mergeEvents(currentEvents, action.payload.event);
      if (nextEvents.length === currentEvents.length) {
        return state;
      }

      const previousTrace = state.entities.tracesById[action.payload.event.trace_id];
      const nextTrace = mergeEventIntoTrace(previousTrace, currentEvents, action.payload.event);
      const nextSession = mergeTraceIntoSession(
        state.entities.sessionsByKey[nextTrace.session_key],
        nextTrace,
        previousTrace,
      );
      const latestCursor = Math.max(
        state.connection.latestCursor ?? action.payload.event.ts,
        action.payload.event.ts,
      );
      const selectedTraceId = state.connection.followLatest
        ? action.payload.event.trace_id
        : state.ui.selectedTraceId;

      // Invalidate backend-computed view model for this trace.
      const nextGroupsById = { ...state.entities.groupsById };
      delete nextGroupsById[action.payload.event.trace_id];

      if (
        previousTrace == null
        && shouldUseFastNewTracePath(state, action.payload.event, nextTrace.session_key)
      ) {
        return {
          ...state,
          connection: {
            ...state.connection,
            latestCursor,
          },
          entities: {
            ...state.entities,
            tracesById: {
              ...state.entities.tracesById,
              [nextTrace.trace_id]: nextTrace,
            },
            eventsByTraceId: {
              ...state.entities.eventsByTraceId,
              [action.payload.event.trace_id]: trimEvents(nextEvents),
            },
            sessionsByKey: nextSession
              ? {
                  ...state.entities.sessionsByKey,
                  [nextSession.session_key]: nextSession,
                }
              : state.entities.sessionsByKey,
            groupsById: nextGroupsById,
          },
          ui: {
            ...state.ui,
            selectedTraceId,
          },
        };
      }

      const nextTracesById = {
        ...state.entities.tracesById,
        [nextTrace.trace_id]: nextTrace,
      };
      const reconciledSelectedTraceId = state.connection.followLatest
        ? pickFollowLatestTraceId(nextTracesById)
        : state.ui.selectedTraceId;
      const entities = pruneEntities({
        ...state.entities,
        tracesById: nextTracesById,
        eventsByTraceId: {
          ...state.entities.eventsByTraceId,
          [action.payload.event.trace_id]: trimEvents(nextEvents),
        },
        sessionsByKey: nextSession
          ? {
              ...state.entities.sessionsByKey,
              [nextSession.session_key]: nextSession,
            }
          : state.entities.sessionsByKey,
        groupsById: nextGroupsById,
      }, reconciledSelectedTraceId);

      return {
        ...state,
        connection: {
          ...state.connection,
          latestCursor,
        },
        entities,
        ui: {
          ...state.ui,
          selectedTraceId: reconciledSelectedTraceId,
        },
      };
    }
    case "append_alert":
      return {
        ...state,
        entities: pruneEntities({
          ...state.entities,
          alertsById: {
            ...state.entities.alertsById,
            [action.payload.alert.alert_id]: action.payload.alert,
          },
        }, state.ui.selectedTraceId),
      };
    case "replace_runtime":
      return {
        ...state,
        runtime: action.payload.runtime,
      };
    case "apply_dashboard_update":
      return {
        ...state,
        dashboardStats: action.payload.dashboardStats,
      };
    case "set_filters":
      if (!hasPartialChange(state.filters, action.payload)) {
        return state;
      }
      return {
        ...state,
        filters: {
          ...state.filters,
          ...action.payload,
        },
      };
    case "set_selected_trace":
      if (state.ui.selectedTraceId === action.payload.traceId) {
        return state;
      }
      return {
        ...state,
        ui: {
          ...state.ui,
          selectedTraceId: action.payload.traceId,
        },
      };
    case "toggle_trace_expanded":
      return {
        ...state,
        ui: {
          ...state.ui,
          expandedTraceIds: {
            ...state.ui.expandedTraceIds,
            [action.payload.traceId]: !state.ui.expandedTraceIds[action.payload.traceId],
          },
        },
      };
    case "toggle_raw_event_expanded":
      return {
        ...state,
        ui: {
          ...state.ui,
          expandedRawEventIds: {
            ...state.ui.expandedRawEventIds,
            [action.payload.eventId]: !state.ui.expandedRawEventIds[action.payload.eventId],
          },
        },
      };
    case "toggle_follow_latest":
      if (
        action.payload?.value !== undefined
        && state.connection.followLatest === action.payload.value
      ) {
        return state;
      }
      return {
        ...state,
        connection: {
          ...state.connection,
          followLatest: action.payload?.value ?? !state.connection.followLatest,
        },
      };
    case "pause_stream":
      return {
        ...state,
        connection: {
          ...state.connection,
          paused: true,
        },
      };
    case "resume_stream":
      return {
        ...state,
        connection: {
          ...state.connection,
          paused: false,
        },
      };
    case "mark_backfill_complete":
      return {
        ...state,
        connection: {
          ...state.connection,
          latestCursor: action.payload.cursor ?? state.connection.latestCursor,
          unreadWhilePaused: 0,
        },
      };
    case "set_active_inspector_section":
      if (state.ui.activeInspectorSection === action.payload.section) {
        return state;
      }
      return {
        ...state,
        ui: {
          ...state.ui,
          activeInspectorSection: action.payload.section,
        },
      };
    case "set_connection_state":
      if (!hasPartialChange(state.connection, action.payload)) {
        return state;
      }
      return {
        ...state,
        connection: {
          ...state.connection,
          ...action.payload,
        },
      };
    case "record_buffered_message":
      return {
        ...state,
        connection: {
          ...state.connection,
          latestCursor: action.payload.cursor ?? state.connection.latestCursor,
          unreadWhilePaused:
            state.connection.unreadWhilePaused + (action.payload.count ?? 1),
        },
      };
    case "clear_observability_data": {
      const initialState = createInitialObservabilityState();
      return {
        ...initialState,
        connection: {
          ...initialState.connection,
          wsConnected: state.connection.wsConnected,
          paused: false,
          followLatest: state.connection.followLatest,
          reconnecting: state.connection.reconnecting,
          latestCursor: null,
          unreadWhilePaused: 0,
        },
        filters: state.filters,
        error: null,
      };
    }
    case "set_error":
      return {
        ...state,
        error: action.payload.error,
      };
    default:
      return state;
  }
}
