import { describe, expect, it } from "vitest";
import type { NapcatEvent, NapcatTrace } from "@/lib/napcat-api";
import { buildTraceDetailViewModel } from "../domain/detail-view";
import { createInitialObservabilityState, observabilityReducer } from "../reducer";
import {
  pickLatestTraceGroup,
  presetFiltersForView,
  selectEventRate10s,
  selectLiveTraceGroups,
  selectSessionTimelineGroups,
} from "../selectors";

function event(overrides: Partial<NapcatEvent>): NapcatEvent {
  return {
    event_id: overrides.event_id ?? Math.random().toString(16).slice(2),
    trace_id: overrides.trace_id ?? "trace_1",
    span_id: overrides.span_id ?? "span_1",
    parent_span_id: overrides.parent_span_id ?? "",
    ts: overrides.ts ?? 100,
    session_key: overrides.session_key ?? "session_1",
    session_id: overrides.session_id ?? "sess_1",
    platform: overrides.platform ?? "napcat",
    chat_type: overrides.chat_type ?? "group",
    chat_id: overrides.chat_id ?? "chat_1",
    user_id: overrides.user_id ?? "user_1",
    message_id: overrides.message_id ?? "msg_1",
    event_type: overrides.event_type ?? "message.received",
    severity: overrides.severity ?? "info",
    payload: overrides.payload ?? {},
  };
}

function trace(overrides: Partial<NapcatTrace>): NapcatTrace {
  return {
    trace_id: overrides.trace_id ?? "trace_1",
    session_key: overrides.session_key ?? "session_1",
    session_id: overrides.session_id ?? "sess_1",
    chat_type: overrides.chat_type ?? "group",
    chat_id: overrides.chat_id ?? "chat_1",
    user_id: overrides.user_id ?? "user_1",
    message_id: overrides.message_id ?? "msg_1",
    started_at: overrides.started_at ?? 100,
    ended_at: overrides.ended_at ?? null,
    event_count: overrides.event_count ?? 0,
    status: overrides.status ?? "in_progress",
    latest_stage: overrides.latest_stage,
    active_stream: overrides.active_stream,
    tool_call_count: overrides.tool_call_count,
    error_count: overrides.error_count,
    model: overrides.model,
    summary: overrides.summary ?? {},
    events: overrides.events,
  };
}

describe("selectors", () => {
  it("ignores no-op filter and selection updates", () => {
    const state = createInitialObservabilityState();
    expect(state.connection.followLatest).toBe(false);

    const sameFilters = observabilityReducer(state, {
      type: "set_filters",
      payload: {},
    });
    expect(sameFilters).toBe(state);

    const selected = observabilityReducer(state, {
      type: "set_selected_trace",
      payload: { traceId: null },
    });
    expect(selected).toBe(state);

    const sameConnection = observabilityReducer(state, {
      type: "set_connection_state",
      payload: { wsConnected: false },
    });
    expect(sameConnection).toBe(state);
  });

  it("maps quick views to filter presets", () => {
    expect(presetFiltersForView("errors").status).toBe("error");
    expect(presetFiltersForView("tools").eventType).toBe("");
    expect(presetFiltersForView("streaming").view).toBe("streaming");
  });

  it("filters ignored traces after reconciling hydrated events", () => {
    let state = createInitialObservabilityState();
    state = observabilityReducer(state, {
      type: "bootstrap_from_snapshot",
      payload: {
        traces: [
          trace({
            trace_id: "trace_ignored",
            event_count: 2,
            status: "completed",
            latest_stage: "final",
          }),
        ],
        events: [
          event({
            trace_id: "trace_ignored",
            ts: 100,
            event_type: "orchestrator.turn.started",
            payload: { message_preview: "hello", stage: "context" },
          }),
          event({
            trace_id: "trace_ignored",
            ts: 101,
            event_type: "orchestrator.turn.ignored",
            payload: { reason: "Ignore", stage: "final" },
          }),
        ],
      },
    });
    state = observabilityReducer(state, {
      type: "set_filters",
      payload: { status: "ignored" },
    });

    const groups = selectLiveTraceGroups(state);

    expect(groups).toHaveLength(1);
    expect(groups[0]?.status).toBe("ignored");
  });

  it("filters trace groups for tools and computes recent event rate", () => {
    let state = createInitialObservabilityState();
    state = observabilityReducer(state, {
      type: "bootstrap_from_snapshot",
      payload: {
        traces: [
          trace({ trace_id: "trace_tools", started_at: 100, event_count: 2, status: "completed", tool_call_count: 1 }),
          trace({ trace_id: "trace_plain", started_at: 90, event_count: 1, status: "completed" }),
        ],
      },
    });
    state = observabilityReducer(state, {
      type: "append_event",
      payload: {
        event: event({ trace_id: "trace_tools", ts: 100, event_type: "agent.tool.called", payload: { tool_name: "read_file", stage: "tools" } }),
      },
    });
    state = observabilityReducer(state, {
      type: "append_event",
      payload: {
        event: event({ trace_id: "trace_tools", ts: 105, event_type: "agent.response.final", payload: { response_preview: "done", stage: "final" } }),
      },
    });
    state = observabilityReducer(state, {
      type: "append_event",
      payload: {
        event: event({ trace_id: "trace_plain", ts: 91, event_type: "agent.response.final", payload: { response_preview: "plain", stage: "final" } }),
      },
    });
    state = observabilityReducer(state, {
      type: "set_connection_state",
      payload: { latestCursor: 108 },
    });
    state = observabilityReducer(state, {
      type: "set_filters",
      payload: presetFiltersForView("tools"),
    });

    const groups = selectLiveTraceGroups(state);
    expect(groups).toHaveLength(1);
    expect(groups[0]?.traceId).toBe("trace_tools");
    expect(selectEventRate10s(state)).toBe(2);
  });

  it("hides system-only adapter lifecycle traces", () => {
    let state = createInitialObservabilityState();
    state = observabilityReducer(state, {
      type: "bootstrap_from_snapshot",
      payload: {
        traces: [
          trace({
            trace_id: "trace_system",
            started_at: 50,
            event_count: 1,
            status: "in_progress",
            chat_id: "",
            message_id: "",
            summary: { headline: "" },
          }),
        ],
      },
    });
    state = observabilityReducer(state, {
      type: "append_event",
      payload: {
        event: event({
          trace_id: "trace_system",
          ts: 50,
          chat_id: "",
          message_id: "",
          event_type: "adapter.lifecycle",
          payload: { action: "disconnected", stage: "raw" },
        }),
      },
    });

    expect(selectLiveTraceGroups(state)).toHaveLength(0);
  });

  it("keeps tool traces visible from summary metadata before events hydrate", () => {
    let state = createInitialObservabilityState();
    state = observabilityReducer(state, {
      type: "bootstrap_from_snapshot",
      payload: {
        traces: [
          trace({
            trace_id: "trace_tools",
            event_count: 1,
            tool_call_count: 2,
            summary: { event_types: ["agent.tool.called"], headline: "tool trace" },
          }),
        ],
      },
    });
    state = observabilityReducer(state, {
      type: "set_filters",
      payload: presetFiltersForView("tools"),
    });

    const groups = selectLiveTraceGroups(state);
    expect(groups).toHaveLength(1);
    expect(groups[0]?.traceId).toBe("trace_tools");
  });

  it("matches child runtime session identifiers in search", () => {
    let state = createInitialObservabilityState();
    state = observabilityReducer(state, {
      type: "bootstrap_from_snapshot",
      payload: {
        traces: [
          trace({
            trace_id: "trace_child",
            event_count: 2,
            summary: { headline: "child trace" },
          }),
        ],
      },
    });
    state = observabilityReducer(state, {
      type: "append_event",
      payload: {
        event: event({
          trace_id: "trace_child",
          ts: 100,
          event_type: "orchestrator.child.spawned",
          payload: {
            child_id: "child_1",
            goal: "analyze repo",
            request_kind: "analysis",
            runtime_session_id: "runtime-child-1",
            runtime_session_key: "agent:main:napcat:dm:chat-1::child_1",
            stage: "tools",
          },
        }),
      },
    });
    state = observabilityReducer(state, {
      type: "append_event",
      payload: {
        event: event({
          trace_id: "trace_child",
          ts: 101,
          event_type: "agent.model.requested",
          payload: {
            child_id: "child_1",
            model: "child-model",
            agent_scope: "orchestrator_child",
            stage: "model",
          },
        }),
      },
    });
    state = observabilityReducer(state, {
      type: "set_filters",
      payload: { search: "runtime-child-1" },
    });

    const groups = selectLiveTraceGroups(state);
    expect(groups).toHaveLength(1);
    expect(groups[0]?.traceId).toBe("trace_child");
  });

  it("groups visible traces by session for tab navigation", () => {
    let state = createInitialObservabilityState();
    state = observabilityReducer(state, {
      type: "bootstrap_from_snapshot",
      payload: {
        traces: [
          trace({ trace_id: "trace_a1", session_key: "session_a", started_at: 100, event_count: 1 }),
          trace({ trace_id: "trace_a2", session_key: "session_a", started_at: 101, event_count: 1 }),
          trace({ trace_id: "trace_b1", session_key: "session_b", started_at: 102, event_count: 1 }),
        ],
      },
    });

    const sessions = selectSessionTimelineGroups(state);
    expect(sessions).toHaveLength(2);
    expect(sessions[0]?.sessionKey).toBe("session_b");
    expect(sessions[0]?.traces).toHaveLength(1);
    expect(sessions[1]?.sessionKey).toBe("session_a");
    expect(sessions[1]?.traces).toHaveLength(2);
  });

  it("does not create a synthetic session bucket for traces without session keys", () => {
    let state = createInitialObservabilityState();
    state = observabilityReducer(state, {
      type: "bootstrap_from_snapshot",
      payload: {
        traces: [
          trace({ trace_id: "trace_a1", session_key: "session_a", started_at: 100, event_count: 1 }),
          trace({ trace_id: "trace_b1", session_key: "session_b", started_at: 102, event_count: 1 }),
          trace({ trace_id: "trace_rejected", session_key: "", session_id: "", started_at: 103, event_count: 1, status: "rejected" }),
        ],
      },
    });

    const sessions = selectSessionTimelineGroups(state);
    expect(sessions).toHaveLength(2);
    expect(sessions.map((session) => session.sessionKey)).toEqual(["session_b", "session_a"]);
  });

  it("picks the most recent trace for follow-latest even when display order prefers streaming traces", () => {
    let state = createInitialObservabilityState();
    state = observabilityReducer(state, {
      type: "bootstrap_from_snapshot",
      payload: {
        traces: [
          trace({
            trace_id: "trace_streaming",
            session_key: "session_streaming",
            started_at: 100,
            event_count: 1,
            active_stream: true,
            latest_stage: "model",
          }),
          trace({
            trace_id: "trace_recent",
            session_key: "session_recent",
            started_at: 105,
            event_count: 1,
            active_stream: false,
            latest_stage: "tools",
          }),
        ],
      },
    });
    state = observabilityReducer(state, {
      type: "append_event",
      payload: {
        event: event({
          trace_id: "trace_streaming",
          session_key: "session_streaming",
          ts: 100,
          event_type: "agent.response.delta",
          payload: { delta: "stream", stage: "model" },
        }),
      },
    });
    state = observabilityReducer(state, {
      type: "append_event",
      payload: {
        event: event({
          trace_id: "trace_recent",
          session_key: "session_recent",
          ts: 110,
          event_type: "agent.tool.called",
          payload: { tool_name: "read_file", stage: "tools" },
        }),
      },
    });

    const groups = selectLiveTraceGroups(state);
    expect(groups[0]?.traceId).toBe("trace_recent");
    expect(pickLatestTraceGroup(groups)?.traceId).toBe("trace_recent");
  });

  it("keeps request-list ordering stable by trace start time while new events arrive", () => {
    let state = createInitialObservabilityState();
    state = observabilityReducer(state, {
      type: "bootstrap_from_snapshot",
      payload: {
        traces: [
          trace({
            trace_id: "trace_older",
            session_key: "session_1",
            started_at: 100,
            event_count: 1,
            active_stream: true,
            latest_stage: "model",
          }),
          trace({
            trace_id: "trace_newer",
            session_key: "session_1",
            started_at: 110,
            event_count: 1,
            active_stream: false,
            latest_stage: "tools",
          }),
        ],
      },
    });
    state = observabilityReducer(state, {
      type: "append_event",
      payload: {
        event: event({
          trace_id: "trace_older",
          session_key: "session_1",
          ts: 200,
          event_type: "agent.response.delta",
          payload: { delta: "still streaming", stage: "model" },
        }),
      },
    });

    const groups = selectLiveTraceGroups(state);
    expect(groups.map((group) => group.traceId)).toEqual(["trace_newer", "trace_older"]);
  });

  it("updates main orchestrator thinking from append_event deltas for the selected trace", () => {
    let state = createInitialObservabilityState();
    state = observabilityReducer(state, {
      type: "append_event",
      payload: {
        event: event({
          trace_id: "trace_orch",
          session_key: "session_orch",
          ts: 100,
          event_type: "orchestrator.turn.started",
          payload: { message_preview: "route this", stage: "context" },
        }),
      },
    });
    state = observabilityReducer(state, {
      type: "append_event",
      payload: {
        event: event({
          trace_id: "trace_orch",
          session_key: "session_orch",
          ts: 101,
          event_type: "agent.model.requested",
          payload: {
            llm_request_id: "req-orch",
            agent_scope: "main_orchestrator",
            model: "router-model",
            stage: "model",
          },
        }),
      },
    });
    state = observabilityReducer(state, {
      type: "set_selected_trace",
      payload: { traceId: "trace_orch" },
    });
    state = observabilityReducer(state, {
      type: "append_event",
      payload: {
        event: event({
          trace_id: "trace_orch",
          session_key: "session_orch",
          ts: 102,
          event_type: "agent.reasoning.delta",
          payload: {
            llm_request_id: "req-orch",
            agent_scope: "main_orchestrator",
            content: "draft reasoning",
            sequence: 1,
            stage: "model",
          },
        }),
      },
    });
    state = observabilityReducer(state, {
      type: "append_event",
      payload: {
        event: event({
          trace_id: "trace_orch",
          session_key: "session_orch",
          ts: 103,
          event_type: "agent.reasoning.delta",
          payload: {
            llm_request_id: "req-orch",
            agent_scope: "main_orchestrator",
            content: "draft reasoning expanded",
            sequence: 2,
            stage: "model",
          },
        }),
      },
    });

    const groups = selectLiveTraceGroups(state);
    const selectedGroup = groups.find((group) => group.traceId === "trace_orch");
    const viewModel = selectedGroup ? buildTraceDetailViewModel(selectedGroup) : null;

    expect(viewModel).not.toBeNull();
    expect(viewModel?.lanes[0]?.key).toBe("main_orchestrator");
    expect(viewModel?.lanes[0]?.mainOrchestratorThinking).toBe("draft reasoning expanded");
  });
});
