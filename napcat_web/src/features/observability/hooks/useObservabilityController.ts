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
  getRuntime,
  getSessions,
  getStats,
  getTraceViewModel,
  getTraceViewModels,
} from "@/lib/napcat-api";
import type {
  FilterState,
  ObservabilityAction,
  ObservabilityController,
  ObservabilityState,
  ObservabilityView,
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
  // The list API may return an incomplete event_count, so we cannot rely on it
  // to determine whether events are complete. Only skip hydration when there
  // are absolutely no events at all.
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

function readFiltersFromUrl(): Partial<FilterState> {
  if (typeof window === "undefined") return {};
  const params = new URLSearchParams(window.location.search);
  const filters: Partial<FilterState> = {};
  const status = params.get("status");
  if (status) filters.status = status as FilterState["status"];
  const eventType = params.get("eventType");
  if (eventType) filters.eventType = eventType;
  const search = params.get("search");
  if (search) filters.search = search;
  const chatId = params.get("chatId");
  if (chatId) filters.chatId = chatId;
  const userId = params.get("userId");
  if (userId) filters.userId = userId;
  const sessionKey = params.get("sessionKey");
  if (sessionKey) filters.sessionKey = sessionKey;
  const view = params.get("view");
  if (view) filters.view = view as ObservabilityView;
  return filters;
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
    case "dashboard.update":
      return null;
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
          groups: options.mode === "detail" ? undefined : message.data.groups,
          traces: options.mode === "detail" ? undefined : message.data.traces,
          sessions: options.mode === "detail" ? undefined : message.data.sessions,
          alerts: message.data.alerts,
          runtime: message.data.runtime,
          stats: message.data.stats,
          cursor: message.data.cursor,
          replaceEntities: false,
        },
      });
      return;
    case "event.append":
      dispatch({ type: "append_event", payload: { event: message.data } });
      return;
    case "trace.update":
      dispatch({ type: "apply_trace_update", payload: { trace: message.data } });
      return;
    case "group.update":
      dispatch({ type: "apply_group_update", payload: { group: message.data } });
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
    case "dashboard.update":
      dispatch({ type: "apply_dashboard_update", payload: { dashboardStats: message.data } });
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
    const urlFilters = readFiltersFromUrl();
    const nextFilters = {
      ...createDefaultFilters(),
      ...(options.initialFilters ?? {}),
      ...urlFilters,
    };
    dispatch({ type: "set_filters", payload: nextFilters });
    if (options.traceId) {
      dispatch({ type: "set_selected_trace", payload: { traceId: options.traceId } });
    }
  }, [options.initialFilters, options.traceId]);

  // Sync filters to URL query string (replaceState to avoid history clutter).
  useEffect(() => {
    if (typeof window === "undefined") return;
    const params = new URLSearchParams(window.location.search);
    if (state.filters.status) params.set("status", state.filters.status);
    else params.delete("status");
    if (state.filters.eventType) params.set("eventType", state.filters.eventType);
    else params.delete("eventType");
    if (state.filters.search) params.set("search", state.filters.search);
    else params.delete("search");
    if (state.filters.chatId) params.set("chatId", state.filters.chatId);
    else params.delete("chatId");
    if (state.filters.userId) params.set("userId", state.filters.userId);
    else params.delete("userId");
    if (state.filters.sessionKey) params.set("sessionKey", state.filters.sessionKey);
    else params.delete("sessionKey");
    if (state.filters.view && state.filters.view !== "all") params.set("view", state.filters.view);
    else params.delete("view");

    const query = params.toString();
    const newUrl = query ? `?${query}` : window.location.pathname;
    if (window.location.search !== newUrl) {
      history.replaceState(null, "", newUrl);
    }
  }, [
    state.filters.status,
    state.filters.eventType,
    state.filters.search,
    state.filters.chatId,
    state.filters.userId,
    state.filters.sessionKey,
    state.filters.view,
  ]);

  const deferredSearch = useDeferredValue(state.filters.search);

  const effectiveFilters = useMemo<FilterState>(() => ({
    ...state.filters,
    search: deferredSearch,
  }), [deferredSearch, state.filters]);

  const runBootstrap = useCallback(async () => {
    try {
      if (optionsRef.current.mode === "detail" && optionsRef.current.traceId) {
        const [groupRes, alertsRes, runtimeRes, statsRes] = await Promise.all([
          getTraceViewModel(optionsRef.current.traceId).catch(() => null),
          getAlerts({ acknowledged: false, limit: 20 }),
          getRuntime(),
          getStats(),
        ]);

        startTransition(() => {
          if (groupRes) {
            dispatch({
              type: "bootstrap_from_snapshot",
              payload: {
                groups: [groupRes],
                alerts: alertsRes.alerts,
                runtime: runtimeRes,
                stats: statsRes,
              },
            });
          } else {
            dispatch({
              type: "set_error",
              payload: { error: "Failed to load trace details. The trace may not exist or the server may be unavailable." },
            });
          }
          dispatch({
            type: "set_selected_trace",
            payload: { traceId: optionsRef.current.traceId! },
          });
        });
        return;
      }

      const [groupsRes, sessionsRes, alertsRes, runtimeRes, statsRes] = await Promise.all([
        getTraceViewModels({
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
        }),
        getSessions({ limit: 20 }),
        getAlerts({ acknowledged: false, limit: 20 }),
        getRuntime(),
        getStats(),
      ]);

      startTransition(() => {
        dispatch({
          type: "bootstrap_from_snapshot",
          payload: {
            groups: groupsRes.groups,
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

    // Wait until initial bootstrap completes before hydrating.
    if (!state.bootstrapped) {
      return;
    }

    // Use stateRef to read latest data without adding eventsByTraceId/tracesById
    // to the dependency array (they change on every reducer run and would cause
    // this effect to fire constantly).
    const currentState = stateRef.current;
    const trace = currentState.entities.tracesById[traceId];
    const events = currentState.entities.eventsByTraceId[traceId];

    // If trace doesn't exist in the current data (e.g. not in the first 80
    // traces returned by the list API), we must load it separately.
    if (!trace) {
      if (hydratingTraceIdsRef.current.has(traceId)) {
        return;
      }
      hydratingTraceIdsRef.current.add(traceId);
      void getTraceViewModel(traceId)
        .then((group) => {
          startTransition(() => {
            dispatch({
              type: "bootstrap_from_snapshot",
              payload: {
                groups: [group],
              },
            });
          });
        })
        .catch((error) => {
          console.warn("Failed to load trace details", traceId, error);
        })
        .finally(() => {
          hydratingTraceIdsRef.current.delete(traceId);
        });
      return;
    }

    // If we already have events, the trace data is complete.
    if ((events?.length ?? 0) > 0) {
      return;
    }

    // Don't re-trigger while a hydration is already in flight.
    if (hydratingTraceIdsRef.current.has(traceId)) {
      return;
    }

    hydratingTraceIdsRef.current.add(traceId);
    void getTraceViewModel(traceId)
      .then((group) => {
        startTransition(() => {
          dispatch({
            type: "bootstrap_from_snapshot",
            payload: {
              groups: [group],
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
  }, [state.bootstrapped, state.ui.selectedTraceId]);

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
    dashboardStats: state.dashboardStats,
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
