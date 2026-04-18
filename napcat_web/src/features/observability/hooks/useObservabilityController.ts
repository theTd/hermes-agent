import {
  startTransition,
  useCallback,
  useDeferredValue,
  useEffect,
  useMemo,
  useReducer,
  useRef,
} from "react";
import type { Dispatch, MutableRefObject } from "react";
import {
  clearAllObservabilityData,
  getAlerts,
  getEvents,
  getRuntime,
  getSessions,
  getStats,
  getTrace,
  getTraces,
} from "@/lib/napcat-api";
import type {
  FilterState,
  ObservabilityAction,
  ObservabilityController,
  ObservabilityState,
} from "../types";
import type { WsIncomingMessage, WsSubscriptionFilters } from "@/lib/napcat-api";
import { createInitialObservabilityState, observabilityReducer } from "../reducer";
import { createDefaultFilters } from "../types";
import { ObservabilityWsClient } from "../ws-client";
import { useObservabilityDerivedData } from "./useObservabilityDerivedData";

const MAX_BUFFERED_MESSAGES = 1000;

interface UseObservabilityControllerOptions {
  mode?: "monitor" | "detail";
  traceId?: string | null;
  initialFilters?: Partial<FilterState>;
  limit?: number;
}

export function shouldHydrateTraceDetails(
  trace: { event_count?: number | null; latest_stage?: string | null; summary?: Record<string, unknown> | null } | null | undefined,
  events: Array<unknown> | undefined,
): boolean {
  if (!trace) {
    return false;
  }
  if ((events?.length ?? 0) > 0) {
    return false;
  }

  const summaryEventTypes = Array.isArray(trace.summary?.event_types)
    ? trace.summary.event_types
    : [];
  return Boolean(
    (trace.event_count ?? 0) > 0
    || String(trace.latest_stage ?? "").trim()
    || summaryEventTypes.length > 0,
  );
}

function toWsFilters(
  filters: FilterState,
  traceId?: string | null,
): WsSubscriptionFilters {
  const remoteStatus = filters.status && filters.status !== "ignored"
    ? filters.status
    : undefined;
  return {
    status: remoteStatus,
    event_type: filters.eventType || undefined,
    search: filters.search || undefined,
    session_key: filters.sessionKey || undefined,
    chat_id: filters.chatId || undefined,
    user_id: filters.userId || undefined,
    view: filters.view,
    trace_id: traceId || undefined,
  };
}

function getMessageCursor(message: WsIncomingMessage): number | null {
  switch (message.type) {
    case "snapshot.init":
      return message.data.cursor ?? null;
    case "event.append":
      return message.data.ts;
    case "trace.update":
      return message.data.ended_at ?? message.data.started_at ?? null;
    case "alert.raised":
      return message.data.ts;
    case "runtime.update": {
      const timestamps = Object.values(message.data).map((entry) => entry.updated_at);
      return timestamps.length > 0 ? Math.max(...timestamps) : null;
    }
    case "backfill.complete":
      return message.data.cursor ?? null;
    default:
      return null;
  }
}

function messageMatchesTraceScope(message: WsIncomingMessage, traceId?: string | null): boolean {
  if (!traceId) {
    return true;
  }
  switch (message.type) {
    case "event.append":
      return message.data.trace_id === traceId;
    case "trace.update":
      return message.data.trace_id === traceId;
    case "alert.raised":
      return message.data.trace_id === traceId;
    default:
      return true;
  }
}

function dispatchWsMessage(
  state: ObservabilityState,
  message: WsIncomingMessage,
  queueRef: MutableRefObject<WsIncomingMessage[]>,
  dispatch: Dispatch<ObservabilityAction>,
  options: UseObservabilityControllerOptions,
): void {
  if (!messageMatchesTraceScope(message, options.traceId)) {
    return;
  }

  if (
    state.connection.paused
    && message.type !== "backfill.complete"
    && message.type !== "snapshot.init"
  ) {
    queueRef.current.push(message);
    if (queueRef.current.length > MAX_BUFFERED_MESSAGES) {
      queueRef.current.shift();
    }
    dispatch({
      type: "record_buffered_message",
      payload: {
        cursor: getMessageCursor(message),
        count: 1,
      },
    });
    return;
  }

  switch (message.type) {
    case "snapshot.init":
      dispatch({
        type: "bootstrap_from_snapshot",
        payload: {
          traces: options.mode === "detail" ? undefined : message.data.traces,
          sessions: options.mode === "detail" ? undefined : message.data.sessions,
          alerts: message.data.alerts,
          runtime: message.data.runtime,
          stats: message.data.stats,
          cursor: message.data.cursor,
          replaceEntities: options.mode !== "detail",
        },
      });
      return;
    case "event.append":
      dispatch({ type: "append_event", payload: { event: message.data } });
      return;
    case "trace.update":
      dispatch({ type: "apply_trace_update", payload: { trace: message.data } });
      return;
    case "runtime.update":
      dispatch({ type: "replace_runtime", payload: { runtime: message.data } });
      return;
    case "alert.raised":
      dispatch({ type: "append_alert", payload: { alert: message.data } });
      return;
    case "backfill.complete":
      dispatch({ type: "mark_backfill_complete", payload: message.data });
      return;
    default:
      return;
  }
}

export function useObservabilityController(
  options: UseObservabilityControllerOptions = {},
): ObservabilityController {
  const [state, dispatch] = useReducer(
    observabilityReducer,
    undefined,
    createInitialObservabilityState,
  );
  const stateRef = useRef(state);
  const optionsRef = useRef(options);
  const wsClientRef = useRef<ObservabilityWsClient | null>(null);
  const bufferedMessagesRef = useRef<WsIncomingMessage[]>([]);
  const pauseCursorRef = useRef<number | null>(null);
  const didHydrateRef = useRef(false);
  const hydratedTraceIdsRef = useRef<Set<string>>(new Set());
  const hydratingTraceIdsRef = useRef<Set<string>>(new Set());

  useEffect(() => {
    stateRef.current = state;
  }, [state]);

  useEffect(() => {
    optionsRef.current = options;
  }, [options]);

  useEffect(() => {
    if (didHydrateRef.current) {
      return;
    }
    didHydrateRef.current = true;
    const nextFilters = {
      ...createDefaultFilters(),
      ...(options.initialFilters ?? {}),
    };
    dispatch({ type: "set_filters", payload: nextFilters });
    if (options.traceId) {
      dispatch({ type: "set_selected_trace", payload: { traceId: options.traceId } });
    }
  }, [options.initialFilters, options.traceId]);

  const deferredSearch = useDeferredValue(state.filters.search);

  const effectiveFilters = useMemo<FilterState>(() => ({
    ...state.filters,
    search: deferredSearch,
  }), [deferredSearch, state.filters]);

  const runBootstrap = useCallback(async () => {
    try {
      if (optionsRef.current.mode === "detail" && optionsRef.current.traceId) {
        const [trace, alertsRes, runtimeRes, statsRes] = await Promise.all([
          getTrace(optionsRef.current.traceId),
          getAlerts({ acknowledged: false, limit: 20 }),
          getRuntime(),
          getStats(),
        ]);

        startTransition(() => {
          dispatch({
            type: "bootstrap_from_snapshot",
            payload: {
              traces: [trace],
              alerts: alertsRes.alerts,
              runtime: runtimeRes,
              stats: statsRes,
            },
          });
          dispatch({
            type: "set_selected_trace",
            payload: { traceId: trace.trace_id },
          });
        });
        return;
      }

      const tracesRes = await getTraces({
        limit: optionsRef.current.limit ?? 80,
        status: effectiveFilters.status && effectiveFilters.status !== "ignored"
          ? effectiveFilters.status
          : undefined,
        session_key: effectiveFilters.sessionKey || undefined,
        chat_id: effectiveFilters.chatId || undefined,
        user_id: effectiveFilters.userId || undefined,
        event_type: effectiveFilters.eventType || undefined,
        search: effectiveFilters.search || undefined,
        view: effectiveFilters.view,
      });

      const traceIds = new Set(tracesRes.traces.map((trace) => trace.trace_id));
      const shouldHydrateEvents = traceIds.size > 0;
      const eventLimit = Math.min(
        2000,
        Math.max(200, traceIds.size * 40),
      );

      const [matchingEventsRes, sessionsRes, alertsRes, runtimeRes, statsRes] = await Promise.all([
        shouldHydrateEvents
          ? getEvents({
              limit: eventLimit,
              session_key: effectiveFilters.sessionKey || undefined,
              chat_id: effectiveFilters.chatId || undefined,
              user_id: effectiveFilters.userId || undefined,
              event_type: effectiveFilters.eventType || undefined,
              search: effectiveFilters.search || undefined,
            })
          : Promise.resolve({ events: [], total: 0 }),
        getSessions({ limit: 20 }),
        getAlerts({ acknowledged: false, limit: 20 }),
        getRuntime(),
        getStats(),
      ]);

      const hydratedEvents = matchingEventsRes.events.filter((event) =>
        traceIds.has(event.trace_id),
      );

      startTransition(() => {
        dispatch({
          type: "bootstrap_from_snapshot",
          payload: {
            traces: tracesRes.traces,
            events: hydratedEvents,
            sessions: sessionsRes.sessions,
            alerts: alertsRes.alerts,
            runtime: runtimeRes,
            stats: statsRes,
            replaceEntities: true,
          },
        });
      });
    } catch (error) {
      const message = error instanceof Error
        ? error.message
        : "Failed to connect to observability service";
      startTransition(() => {
        dispatch({ type: "set_error", payload: { error: message } });
      });
    }
  }, [
    effectiveFilters.chatId,
    effectiveFilters.eventType,
    effectiveFilters.search,
    effectiveFilters.sessionKey,
    effectiveFilters.status,
    effectiveFilters.userId,
    effectiveFilters.view,
    options.limit,
    options.mode,
    options.traceId,
  ]);

  useEffect(() => {
    void runBootstrap();
  }, [runBootstrap]);

  useEffect(() => {
    const traceId = state.ui.selectedTraceId;
    if (!traceId) {
      return;
    }

    const trace = state.entities.tracesById[traceId];
    const events = state.entities.eventsByTraceId[traceId];
    if (!shouldHydrateTraceDetails(trace, events)) {
      if ((events?.length ?? 0) > 0) {
        hydratedTraceIdsRef.current.add(traceId);
      }
      return;
    }
    if (hydratedTraceIdsRef.current.has(traceId) || hydratingTraceIdsRef.current.has(traceId)) {
      return;
    }

    hydratingTraceIdsRef.current.add(traceId);
    void getTrace(traceId)
      .then((fullTrace) => {
        hydratedTraceIdsRef.current.add(traceId);
        startTransition(() => {
          dispatch({
            type: "bootstrap_from_snapshot",
            payload: {
              traces: [fullTrace],
            },
          });
        });
      })
      .catch((error) => {
        console.warn("Failed to hydrate trace details", traceId, error);
      })
      .finally(() => {
        hydratingTraceIdsRef.current.delete(traceId);
      });
  }, [state.entities.eventsByTraceId, state.entities.tracesById, state.ui.selectedTraceId]);

  useEffect(() => {
    const client = new ObservabilityWsClient({
      onMessage: (message) => {
        startTransition(() => {
          dispatchWsMessage(stateRef.current, message, bufferedMessagesRef, dispatch, optionsRef.current);
        });
      },
      onConnectionStateChange: (connectionState) => {
        const wasConnected = stateRef.current.connection.wsConnected;
        startTransition(() => {
          dispatch({
            type: "set_connection_state",
            payload: connectionState,
          });
        });
        if (
          connectionState.wsConnected
          && !wasConnected
          && !stateRef.current.connection.paused
          && stateRef.current.connection.latestCursor != null
        ) {
          wsClientRef.current?.backfill(
            stateRef.current.connection.latestCursor,
            toWsFilters(stateRef.current.filters, optionsRef.current.traceId),
          );
        }
      },
    });
    wsClientRef.current = client;
    client.connect();

    return () => {
      client.disconnect();
      wsClientRef.current = null;
    };
  }, []);

  const wsFilters = useMemo(
    () => toWsFilters(effectiveFilters, options.traceId),
    [effectiveFilters, options.traceId],
  );

  useEffect(() => {
    wsClientRef.current?.subscribe(wsFilters);
  }, [wsFilters]);

  const {
    groups,
    traces,
    sessions,
    alerts,
    selectedGroup,
    selectedTrace,
    selectedTraceEvents,
    eventRate10s,
  } = useObservabilityDerivedData(state);

  return useMemo(() => ({
    state,
    traces,
    groups,
    sessions,
    alerts,
    selectedTrace,
    selectedTraceEvents,
    selectedGroup,
    eventRate10s,
    setFilters: (patch) => {
      dispatch({ type: "set_filters", payload: patch });
    },
    setSelectedTrace: (traceId) => {
      dispatch({ type: "set_selected_trace", payload: { traceId } });
    },
    toggleTraceExpanded: (traceId) => {
      dispatch({ type: "toggle_trace_expanded", payload: { traceId } });
    },
    toggleRawEventExpanded: (eventId) => {
      dispatch({ type: "toggle_raw_event_expanded", payload: { eventId } });
    },
    toggleFollowLatest: (value) => {
      dispatch({ type: "toggle_follow_latest", payload: { value } });
    },
    pauseStream: () => {
      pauseCursorRef.current = stateRef.current.connection.latestCursor;
      dispatch({ type: "pause_stream" });
    },
    resumeStream: () => {
      const resumedState: ObservabilityState = {
        ...stateRef.current,
        connection: {
          ...stateRef.current.connection,
          paused: false,
        },
      };
      stateRef.current = resumedState;
      dispatch({ type: "resume_stream" });

      const bufferedMessages = bufferedMessagesRef.current;
      bufferedMessagesRef.current = [];
      for (const message of bufferedMessages) {
        dispatchWsMessage(
          resumedState,
          message,
          bufferedMessagesRef,
          dispatch,
          optionsRef.current,
        );
      }

      const cursor = pauseCursorRef.current ?? resumedState.connection.latestCursor;
      if (cursor != null) {
        wsClientRef.current?.backfill(cursor, toWsFilters(resumedState.filters, optionsRef.current.traceId));
      } else {
        dispatch({
          type: "mark_backfill_complete",
          payload: { cursor: null, count: 0 },
        });
      }
      pauseCursorRef.current = null;
    },
    setActiveInspectorSection: (section) => {
      dispatch({
        type: "set_active_inspector_section",
        payload: { section },
      });
    },
    reconnect: () => {
      dispatch({ type: "set_error", payload: { error: null } });
      void runBootstrap();
      wsClientRef.current?.disconnect();
      wsClientRef.current?.connect();
    },
    clearFilters: () => {
      dispatch({ type: "set_filters", payload: createDefaultFilters() });
    },
    clearAllData: async () => {
      dispatch({ type: "set_error", payload: { error: null } });
      try {
        await clearAllObservabilityData();
        bufferedMessagesRef.current = [];
        pauseCursorRef.current = null;
        hydratedTraceIdsRef.current.clear();
        hydratingTraceIdsRef.current.clear();
        startTransition(() => {
          dispatch({ type: "clear_observability_data" });
        });
        wsClientRef.current?.disconnect();
        wsClientRef.current?.connect();
        void runBootstrap();
      } catch (error) {
        const message = error instanceof Error ? error.message : "Failed to clear observability data";
        dispatch({ type: "set_error", payload: { error: message } });
      }
    },
  }), [
    alerts,
    eventRate10s,
    groups,
    runBootstrap,
    selectedGroup,
    selectedTrace,
    selectedTraceEvents,
    sessions,
    state,
    traces,
  ]);
}
