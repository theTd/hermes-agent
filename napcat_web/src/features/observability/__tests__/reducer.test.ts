import { describe, expect, it } from "vitest";

import { createInitialObservabilityState, observabilityReducer } from "../reducer";

describe("observabilityReducer", () => {
  it("clears persisted observability entities while preserving connection mode and filters", () => {
    const initial = createInitialObservabilityState();
    const state = {
      ...initial,
      connection: {
        ...initial.connection,
        wsConnected: true,
        paused: true,
        followLatest: true,
        reconnecting: true,
        latestCursor: 123,
        unreadWhilePaused: 8,
      },
      filters: {
        ...initial.filters,
        search: "boom",
        status: "error",
      },
      entities: {
        tracesById: {
          "trace-1": {
            trace_id: "trace-1",
            session_key: "session-1",
            session_id: "sess-1",
            chat_type: "group",
            chat_id: "chat-1",
            user_id: "user-1",
            message_id: "msg-1",
            started_at: 1,
            ended_at: null,
            event_count: 1,
            status: "in_progress",
            summary: {},
          },
        },
        eventsByTraceId: {
          "trace-1": [
            {
              event_id: "evt-1",
              trace_id: "trace-1",
              span_id: "span-1",
              parent_span_id: "",
              ts: 1,
              session_key: "session-1",
              session_id: "sess-1",
              platform: "napcat",
              chat_type: "group",
              chat_id: "chat-1",
              user_id: "user-1",
              message_id: "msg-1",
              event_type: "message.received",
              severity: "info",
              payload: {},
            },
          ],
        },
        alertsById: {
          "alert-1": {
            alert_id: "alert-1",
            trace_id: "trace-1",
            event_id: "evt-1",
            ts: 1,
            severity: "error",
            title: "boom",
            detail: "detail",
            acknowledged: 0,
          },
        },
        sessionsByKey: {
          "session-1": {
            session_key: "session-1",
            session_id: "sess-1",
            trace_count: 1,
            first_seen: 1,
            last_seen: 1,
            total_events: 1,
          },
        },
        groupsById: {},
      },
      error: "old error",
    };

    const next = observabilityReducer(state, { type: "clear_observability_data" });

    expect(next.entities.tracesById).toEqual({});
    expect(next.entities.eventsByTraceId).toEqual({});
    expect(next.entities.alertsById).toEqual({});
    expect(next.entities.sessionsByKey).toEqual({});
    expect(next.connection.wsConnected).toBe(true);
    expect(next.connection.followLatest).toBe(true);
    expect(next.connection.paused).toBe(false);
    expect(next.connection.latestCursor).toBeNull();
    expect(next.connection.unreadWhilePaused).toBe(0);
    expect(next.filters.search).toBe("boom");
    expect(next.filters.status).toBe("error");
    expect(next.error).toBeNull();
  });

  it("materializes a trace shell immediately on first event.append", () => {
    const initial = createInitialObservabilityState();

    const next = observabilityReducer(initial, {
      type: "append_event",
      payload: {
        event: {
          event_id: "evt-1",
          trace_id: "trace-1",
          span_id: "span-1",
          parent_span_id: "",
          ts: 10,
          session_key: "session-1",
          session_id: "sess-1",
          platform: "napcat",
          chat_type: "group",
          chat_id: "chat-1",
          user_id: "user-1",
          message_id: "msg-1",
          event_type: "message.received",
          severity: "info",
          payload: { text_preview: "hello", stage: "inbound" },
        },
      },
    });

    expect(next.entities.tracesById["trace-1"]).toEqual(
      expect.objectContaining({
        trace_id: "trace-1",
        session_key: "session-1",
        event_count: 1,
        status: "in_progress",
      }),
    );
    expect(next.entities.eventsByTraceId["trace-1"]).toHaveLength(1);
  });

  it("materializes a trace immediately on trace.created without waiting for trace.update", () => {
    const initial = createInitialObservabilityState();

    const next = observabilityReducer(initial, {
      type: "append_event",
      payload: {
        event: {
          event_id: "evt-created",
          trace_id: "trace-created",
          span_id: "span-created",
          parent_span_id: "",
          ts: 5,
          session_key: "",
          session_id: "",
          platform: "napcat",
          chat_type: "private",
          chat_id: "chat-early",
          user_id: "user-early",
          message_id: "msg-early",
          event_type: "trace.created",
          severity: "info",
          payload: { message_type: "private", stage: "inbound" },
        },
      },
    });

    expect(next.entities.tracesById["trace-created"]).toEqual(
      expect.objectContaining({
        trace_id: "trace-created",
        chat_id: "chat-early",
        event_count: 1,
        status: "in_progress",
      }),
    );
    expect(next.entities.eventsByTraceId["trace-created"]).toHaveLength(1);
  });

  it("derives ignored status from orchestrator terminal events", () => {
    let state = createInitialObservabilityState();

    state = observabilityReducer(state, {
      type: "append_event",
      payload: {
        event: {
          event_id: "evt-1",
          trace_id: "trace-1",
          span_id: "span-1",
          parent_span_id: "",
          ts: 10,
          session_key: "session-1",
          session_id: "sess-1",
          platform: "napcat",
          chat_type: "group",
          chat_id: "chat-1",
          user_id: "user-1",
          message_id: "msg-1",
          event_type: "orchestrator.turn.started",
          severity: "info",
          payload: { message_preview: "hello", stage: "context" },
        },
      },
    });
    state = observabilityReducer(state, {
      type: "append_event",
      payload: {
        event: {
          event_id: "evt-2",
          trace_id: "trace-1",
          span_id: "span-2",
          parent_span_id: "span-1",
          ts: 11,
          session_key: "session-1",
          session_id: "sess-1",
          platform: "napcat",
          chat_type: "group",
          chat_id: "chat-1",
          user_id: "user-1",
          message_id: "msg-1",
          event_type: "orchestrator.turn.ignored",
          severity: "info",
          payload: { reason: "Not agent-related", stage: "final" },
        },
      },
    });

    expect(state.entities.tracesById["trace-1"]).toEqual(
      expect.objectContaining({
        status: "ignored",
        ended_at: 11,
      }),
    );
  });

  it("preserves current selection when a new trace arrives and follow latest is disabled", () => {
    let state = createInitialObservabilityState();
    state = observabilityReducer(state, {
      type: "bootstrap_from_snapshot",
      payload: {
        traces: [
          {
            trace_id: "trace-current",
            session_key: "session-1",
            session_id: "sess-1",
            chat_type: "group",
            chat_id: "chat-1",
            user_id: "user-1",
            message_id: "msg-1",
            started_at: 1,
            ended_at: null,
            event_count: 1,
            status: "in_progress",
            summary: {},
          },
        ],
      },
    });
    state = observabilityReducer(state, {
      type: "set_selected_trace",
      payload: { traceId: "trace-current" },
    });

    const next = observabilityReducer(state, {
      type: "append_event",
      payload: {
        event: {
          event_id: "evt-new",
          trace_id: "trace-new",
          span_id: "span-new",
          parent_span_id: "",
          ts: 5,
          session_key: "session-2",
          session_id: "sess-2",
          platform: "napcat",
          chat_type: "group",
          chat_id: "chat-2",
          user_id: "user-2",
          message_id: "msg-2",
          event_type: "message.received",
          severity: "info",
          payload: { text_preview: "new trace", stage: "inbound" },
        },
      },
    });

    expect(next.ui.selectedTraceId).toBe("trace-current");
  });

  it("switches to the newest trace when follow latest is enabled", () => {
    let state = createInitialObservabilityState();
    state = observabilityReducer(state, {
      type: "toggle_follow_latest",
      payload: { value: true },
    });
    state = observabilityReducer(state, {
      type: "bootstrap_from_snapshot",
      payload: {
        traces: [
          {
            trace_id: "trace-current",
            session_key: "session-1",
            session_id: "sess-1",
            chat_type: "group",
            chat_id: "chat-1",
            user_id: "user-1",
            message_id: "msg-1",
            started_at: 1,
            ended_at: null,
            event_count: 1,
            status: "in_progress",
            summary: {},
          },
        ],
      },
    });

    const next = observabilityReducer(state, {
      type: "append_event",
      payload: {
        event: {
          event_id: "evt-new",
          trace_id: "trace-new",
          span_id: "span-new",
          parent_span_id: "",
          ts: 5,
          session_key: "session-2",
          session_id: "sess-2",
          platform: "napcat",
          chat_type: "group",
          chat_id: "chat-2",
          user_id: "user-2",
          message_id: "msg-2",
          event_type: "message.received",
          severity: "info",
          payload: { text_preview: "new trace", stage: "inbound" },
        },
      },
    });

    expect(next.ui.selectedTraceId).toBe("trace-new");
  });

  it("replaces stale session traces on full snapshot bootstrap", () => {
    let state = createInitialObservabilityState();
    state = observabilityReducer(state, {
      type: "bootstrap_from_snapshot",
      payload: {
        traces: [
          {
            trace_id: "trace-a",
            session_key: "session-a",
            session_id: "sess-a",
            chat_type: "group",
            chat_id: "chat-a",
            user_id: "user-a",
            message_id: "msg-a",
            started_at: 1,
            ended_at: null,
            event_count: 1,
            status: "in_progress",
            summary: {},
          },
          {
            trace_id: "trace-b",
            session_key: "session-b",
            session_id: "sess-b",
            chat_type: "group",
            chat_id: "chat-b",
            user_id: "user-b",
            message_id: "msg-b",
            started_at: 2,
            ended_at: null,
            event_count: 1,
            status: "in_progress",
            summary: {},
          },
        ],
        events: [
          {
            event_id: "evt-a",
            trace_id: "trace-a",
            span_id: "span-a",
            parent_span_id: "",
            ts: 1,
            session_key: "session-a",
            session_id: "sess-a",
            platform: "napcat",
            chat_type: "group",
            chat_id: "chat-a",
            user_id: "user-a",
            message_id: "msg-a",
            event_type: "message.received",
            severity: "info",
            payload: { stage: "inbound" },
          },
          {
            event_id: "evt-b",
            trace_id: "trace-b",
            span_id: "span-b",
            parent_span_id: "",
            ts: 2,
            session_key: "session-b",
            session_id: "sess-b",
            platform: "napcat",
            chat_type: "group",
            chat_id: "chat-b",
            user_id: "user-b",
            message_id: "msg-b",
            event_type: "message.received",
            severity: "info",
            payload: { stage: "inbound" },
          },
        ],
        sessions: [
          {
            session_key: "session-a",
            session_id: "sess-a",
            trace_count: 1,
            first_seen: 1,
            last_seen: 1,
            total_events: 1,
          },
          {
            session_key: "session-b",
            session_id: "sess-b",
            trace_count: 1,
            first_seen: 2,
            last_seen: 2,
            total_events: 1,
          },
        ],
        replaceEntities: true,
      },
    });

    const next = observabilityReducer(state, {
      type: "bootstrap_from_snapshot",
      payload: {
        traces: [
          {
            trace_id: "trace-a",
            session_key: "session-a",
            session_id: "sess-a",
            chat_type: "group",
            chat_id: "chat-a",
            user_id: "user-a",
            message_id: "msg-a",
            started_at: 1,
            ended_at: 3,
            event_count: 2,
            status: "completed",
            summary: {},
          },
        ],
        events: [
          {
            event_id: "evt-a-2",
            trace_id: "trace-a",
            span_id: "span-a-2",
            parent_span_id: "",
            ts: 3,
            session_key: "session-a",
            session_id: "sess-a",
            platform: "napcat",
            chat_type: "group",
            chat_id: "chat-a",
            user_id: "user-a",
            message_id: "msg-a",
            event_type: "agent.response.final",
            severity: "info",
            payload: { stage: "final" },
          },
        ],
        sessions: [
          {
            session_key: "session-a",
            session_id: "sess-a",
            trace_count: 1,
            first_seen: 1,
            last_seen: 3,
            total_events: 2,
          },
        ],
        replaceEntities: true,
      },
    });

    expect(Object.keys(next.entities.tracesById)).toEqual(["trace-a"]);
    expect(Object.keys(next.entities.eventsByTraceId)).toEqual(["trace-a"]);
    expect(Object.keys(next.entities.sessionsByKey)).toEqual(["session-a"]);
    expect(next.entities.eventsByTraceId["trace-a"]).toHaveLength(2);
  });

  it("merges single-trace hydration without clearing other traces", () => {
    let state = createInitialObservabilityState();
    state = observabilityReducer(state, {
      type: "bootstrap_from_snapshot",
      payload: {
        traces: [
          {
            trace_id: "trace-a",
            session_key: "session-a",
            session_id: "sess-a",
            chat_type: "group",
            chat_id: "chat-a",
            user_id: "user-a",
            message_id: "msg-a",
            started_at: 1,
            ended_at: null,
            event_count: 1,
            status: "in_progress",
            summary: {},
          },
          {
            trace_id: "trace-b",
            session_key: "session-b",
            session_id: "sess-b",
            chat_type: "group",
            chat_id: "chat-b",
            user_id: "user-b",
            message_id: "msg-b",
            started_at: 2,
            ended_at: null,
            event_count: 1,
            status: "in_progress",
            summary: {},
          },
        ],
        replaceEntities: true,
      },
    });

    const next = observabilityReducer(state, {
      type: "bootstrap_from_snapshot",
      payload: {
        traces: [
          {
            trace_id: "trace-a",
            session_key: "session-a",
            session_id: "sess-a",
            chat_type: "group",
            chat_id: "chat-a",
            user_id: "user-a",
            message_id: "msg-a",
            started_at: 1,
            ended_at: 4,
            event_count: 3,
            status: "completed",
            summary: { headline: "hydrated" },
            events: [
              {
                event_id: "evt-a",
                trace_id: "trace-a",
                span_id: "span-a",
                parent_span_id: "",
                ts: 1,
                session_key: "session-a",
                session_id: "sess-a",
                platform: "napcat",
                chat_type: "group",
                chat_id: "chat-a",
                user_id: "user-a",
                message_id: "msg-a",
                event_type: "message.received",
                severity: "info",
                payload: { stage: "inbound" },
              },
            ],
          },
        ],
      },
    });

    expect(Object.keys(next.entities.tracesById).sort()).toEqual(["trace-a", "trace-b"]);
    expect(next.entities.tracesById["trace-a"]).toEqual(
      expect.objectContaining({
        ended_at: 4,
        event_count: 3,
        status: "completed",
      }),
    );
    expect(next.entities.tracesById["trace-b"]).toBeDefined();
  });

  it("does not switch follow-latest selection to an older trace just because it emitted a new event", () => {
    let state = createInitialObservabilityState();
    state = observabilityReducer(state, {
      type: "toggle_follow_latest",
      payload: { value: true },
    });
    state = observabilityReducer(state, {
      type: "bootstrap_from_snapshot",
      payload: {
        traces: [
          {
            trace_id: "trace-old",
            session_key: "session-1",
            session_id: "sess-1",
            chat_type: "group",
            chat_id: "chat-1",
            user_id: "user-1",
            message_id: "msg-1",
            started_at: 1,
            ended_at: null,
            event_count: 1,
            status: "in_progress",
            summary: {},
          },
          {
            trace_id: "trace-new",
            session_key: "session-2",
            session_id: "sess-2",
            chat_type: "group",
            chat_id: "chat-2",
            user_id: "user-2",
            message_id: "msg-2",
            started_at: 10,
            ended_at: null,
            event_count: 1,
            status: "in_progress",
            summary: {},
          },
        ],
      },
    });

    const next = observabilityReducer(state, {
      type: "append_event",
      payload: {
        event: {
          event_id: "evt-old",
          trace_id: "trace-old",
          span_id: "span-old",
          parent_span_id: "",
          ts: 20,
          session_key: "session-1",
          session_id: "sess-1",
          platform: "napcat",
          chat_type: "group",
          chat_id: "chat-1",
          user_id: "user-1",
          message_id: "msg-1",
          event_type: "agent.response.delta",
          severity: "info",
          payload: { delta: "still running", stage: "model" },
        },
      },
    });

    expect(state.ui.selectedTraceId).toBe("trace-new");
    expect(next.ui.selectedTraceId).toBe("trace-new");
  });

  it("keeps a trace in progress while other child tasks are still active", () => {
    let state = createInitialObservabilityState();

    state = observabilityReducer(state, {
      type: "append_event",
      payload: {
        event: {
          event_id: "evt-start",
          trace_id: "trace-1",
          span_id: "span-1",
          parent_span_id: "",
          ts: 1,
          session_key: "session-1",
          session_id: "sess-1",
          platform: "napcat",
          chat_type: "group",
          chat_id: "chat-1",
          user_id: "user-1",
          message_id: "msg-1",
          event_type: "orchestrator.child.spawned",
          severity: "info",
          payload: { child_id: "child-1", stage: "tools" },
        },
      },
    });
    state = observabilityReducer(state, {
      type: "append_event",
      payload: {
        event: {
          event_id: "evt-start-2",
          trace_id: "trace-1",
          span_id: "span-2",
          parent_span_id: "",
          ts: 2,
          session_key: "session-1",
          session_id: "sess-1",
          platform: "napcat",
          chat_type: "group",
          chat_id: "chat-1",
          user_id: "user-1",
          message_id: "msg-1",
          event_type: "orchestrator.child.spawned",
          severity: "info",
          payload: { child_id: "child-2", stage: "tools" },
        },
      },
    });

    const next = observabilityReducer(state, {
      type: "append_event",
      payload: {
        event: {
          event_id: "evt-complete",
          trace_id: "trace-1",
          span_id: "span-3",
          parent_span_id: "",
          ts: 3,
          session_key: "session-1",
          session_id: "sess-1",
          platform: "napcat",
          chat_type: "group",
          chat_id: "chat-1",
          user_id: "user-1",
          message_id: "msg-1",
          event_type: "orchestrator.child.completed",
          severity: "info",
          payload: { child_id: "child-1", stage: "final" },
        },
      },
    });

    expect(next.entities.tracesById["trace-1"]?.status).toBe("in_progress");
    expect(next.entities.tracesById["trace-1"]?.ended_at).toBeNull();
  });
});
