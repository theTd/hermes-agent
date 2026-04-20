import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import type { ObservabilityController } from "../../types";
import { mapTraceToLiveGroup } from "../../stage-mapper";
import { TraceInspector } from "../TraceInspector";

function buildControllerWithLongTrace(): ObservabilityController {
  const group = mapTraceToLiveGroup(
    {
      trace_id: "trace-1",
      session_key: "session-1",
      session_id: "sess-1",
      chat_type: "group",
      chat_id: "chat-1",
      user_id: "user-1",
      message_id: "msg-1",
      started_at: 1,
      ended_at: 3,
      event_count: 4,
      status: "completed",
      latest_stage: "final",
      summary: { headline: "long trace" },
    },
    [
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
        event_type: "agent.model.requested",
        severity: "info",
        payload: {
          llm_request_id: "req-1",
          model: "gpt-test",
          provider: "openai",
          request_body: "REQ ".repeat(120),
          stage: "model",
        },
      },
      {
        event_id: "evt-2",
        trace_id: "trace-1",
        span_id: "span-2",
        parent_span_id: "span-1",
        ts: 1.5,
        session_key: "session-1",
        session_id: "sess-1",
        platform: "napcat",
        chat_type: "group",
        chat_id: "chat-1",
        user_id: "user-1",
        message_id: "msg-1",
        event_type: "agent.reasoning.delta",
        severity: "info",
        payload: {
          llm_request_id: "req-1",
          content: "THINK ".repeat(120),
          sequence: 1,
          stage: "model",
        },
      },
      {
        event_id: "evt-3",
        trace_id: "trace-1",
        span_id: "span-3",
        parent_span_id: "span-1",
        ts: 2,
        session_key: "session-1",
        session_id: "sess-1",
        platform: "napcat",
        chat_type: "group",
        chat_id: "chat-1",
        user_id: "user-1",
        message_id: "msg-1",
        event_type: "agent.response.delta",
        severity: "info",
        payload: {
          llm_request_id: "req-1",
          content: "RESP ".repeat(120),
          sequence: 1,
          stage: "model",
        },
      },
      {
        event_id: "evt-4",
        trace_id: "trace-1",
        span_id: "span-4",
        parent_span_id: "span-1",
        ts: 3,
        session_key: "session-1",
        session_id: "sess-1",
        platform: "napcat",
        chat_type: "group",
        chat_id: "chat-1",
        user_id: "user-1",
        message_id: "msg-1",
        event_type: "agent.model.completed",
        severity: "info",
        payload: {
          llm_request_id: "req-1",
          model: "gpt-test",
          provider: "openai",
          total_tokens: 123,
          duration_ms: 2000,
          stage: "model",
        },
      },
    ],
  );

  return {
    state: {
      connection: {
        wsConnected: true,
        paused: false,
        followLatest: false,
        reconnecting: false,
        latestCursor: 3,
        unreadWhilePaused: 0,
      },
      filters: {
        status: "",
        eventType: "",
        search: "",
        sessionKey: "",
        chatId: "",
        userId: "",
        view: "all",
      },
      entities: {
        tracesById: {},
        eventsByTraceId: {},
        alertsById: {},
        sessionsByKey: {},
        groupsById: {},
      },
      runtime: {},
      stats: null,
      dashboardStats: null,
      ui: {
        selectedTraceId: "trace-1",
        expandedTraceIds: {},
        expandedRawEventIds: {},
        activeInspectorSection: null,
      },
      bootstrapped: true,
      error: null,
    },
    traces: [group.trace],
    groups: [group],
    sessions: [],
    alerts: [],
    selectedTrace: group.trace,
    selectedTraceEvents: group.rawEvents,
    selectedGroup: group,
    eventRate10s: 0,
    dashboardStats: null,
    setFilters: () => {},
    setSelectedTrace: () => {},
    toggleTraceExpanded: () => {},
    toggleRawEventExpanded: () => {},
    toggleFollowLatest: () => {},
    pauseStream: () => {},
    resumeStream: () => {},
    setActiveInspectorSection: () => {},
    reconnect: () => {},
    clearFilters: () => {},
    clearAllData: async () => {},
  };
}

function buildControllerWithStandaloneMemoryEvent(): ObservabilityController {
  const group = mapTraceToLiveGroup(
    {
      trace_id: "trace-memory",
      session_key: "session-memory",
      session_id: "sess-memory",
      chat_type: "group",
      chat_id: "chat-memory",
      user_id: "user-memory",
      message_id: "msg-memory",
      started_at: 1,
      ended_at: 2,
      event_count: 1,
      status: "completed",
      latest_stage: "memory_skill_routing",
      summary: { headline: "memory trace" },
    },
    [
      {
        event_id: "evt-memory",
        trace_id: "trace-memory",
        span_id: "span-memory",
        parent_span_id: "",
        ts: 1,
        session_key: "session-memory",
        session_id: "sess-memory",
        platform: "napcat",
        chat_type: "group",
        chat_id: "chat-memory",
        user_id: "user-memory",
        message_id: "msg-memory",
        event_type: "agent.memory.used",
        severity: "info",
        payload: {
          source: "auto_injection",
          summary: "Prefetched identity memory",
          stage: "memory_skill_routing",
          memory_prefetch_fenced: "<memory-context>Alice</memory-context>",
        },
      },
    ],
  );

  return {
    state: {
      connection: {
        wsConnected: true,
        paused: false,
        followLatest: false,
        reconnecting: false,
        latestCursor: 1,
        unreadWhilePaused: 0,
      },
      filters: {
        status: "",
        eventType: "",
        search: "",
        sessionKey: "",
        chatId: "",
        userId: "",
        view: "all",
      },
      entities: {
        tracesById: {},
        eventsByTraceId: {},
        alertsById: {},
        sessionsByKey: {},
        groupsById: {},
      },
      runtime: {},
      stats: null,
      dashboardStats: null,
      ui: {
        selectedTraceId: "trace-memory",
        expandedTraceIds: {},
        expandedRawEventIds: {},
        activeInspectorSection: null,
      },
      bootstrapped: true,
      error: null,
    },
    traces: [group.trace],
    groups: [group],
    sessions: [],
    alerts: [],
    selectedTrace: group.trace,
    selectedTraceEvents: group.rawEvents,
    selectedGroup: group,
    eventRate10s: 0,
    dashboardStats: null,
    setFilters: () => {},
    setSelectedTrace: () => {},
    toggleTraceExpanded: () => {},
    toggleRawEventExpanded: () => {},
    toggleFollowLatest: () => {},
    pauseStream: () => {},
    resumeStream: () => {},
    setActiveInspectorSection: () => {},
    reconnect: () => {},
    clearFilters: () => {},
    clearAllData: async () => {},
  };
}

function buildControllerWithMixedContextDebug(): ObservabilityController {
  const group = mapTraceToLiveGroup(
    {
      trace_id: "trace-context",
      session_key: "session-context",
      session_id: "sess-context",
      chat_type: "group",
      chat_id: "chat-context",
      user_id: "user-context",
      message_id: "msg-context",
      started_at: 1,
      ended_at: 2,
      event_count: 1,
      status: "completed",
      latest_stage: "context",
      summary: { headline: "context trace" },
    },
    [
      {
        event_id: "evt-context",
        trace_id: "trace-context",
        span_id: "span-context",
        parent_span_id: "",
        ts: 1,
        session_key: "session-context",
        session_id: "sess-context",
        platform: "napcat",
        chat_type: "group",
        chat_id: "chat-context",
        user_id: "user-context",
        message_id: "msg-context",
        event_type: "agent.memory.used",
        severity: "info",
        payload: {
          source: "auto_injection",
          stage: "context",
          memory_prefetch_fenced: "<memory-context>Alice</memory-context>",
          memory_prefetch_params_by_provider: "## lightrag\n{\n  \"lane\": \"identity\",\n  \"query_mode\": \"mix\"\n}",
        },
      },
    ],
  );

  return {
    ...buildControllerWithLongTrace(),
    traces: [group.trace],
    groups: [group],
    selectedTrace: group.trace,
    selectedTraceEvents: group.rawEvents,
    selectedGroup: group,
  };
}

function buildControllerWithIgnoredTrace(): ObservabilityController {
  const group = mapTraceToLiveGroup(
    {
      trace_id: "trace-ignored",
      session_key: "session-ignored",
      session_id: "sess-ignored",
      chat_type: "group",
      chat_id: "chat-ignored",
      user_id: "user-ignored",
      message_id: "msg-ignored",
      started_at: 1,
      ended_at: 4,
      event_count: 4,
      status: "ignored",
      latest_stage: "final",
      summary: { headline: "ignored trace" },
    },
    [
      {
        event_id: "evt-ignored-1",
        trace_id: "trace-ignored",
        span_id: "span-ignored-1",
        parent_span_id: "",
        ts: 1,
        session_key: "session-ignored",
        session_id: "sess-ignored",
        platform: "napcat",
        chat_type: "group",
        chat_id: "chat-ignored",
        user_id: "user-ignored",
        message_id: "msg-ignored",
        event_type: "message.received",
        severity: "info",
        payload: {
          text_preview: "people chatting nearby",
          stage: "inbound",
        },
      },
      {
        event_id: "evt-ignored-2",
        trace_id: "trace-ignored",
        span_id: "span-ignored-2",
        parent_span_id: "span-ignored-1",
        ts: 2,
        session_key: "session-ignored",
        session_id: "sess-ignored",
        platform: "napcat",
        chat_type: "group",
        chat_id: "chat-ignored",
        user_id: "user-ignored",
        message_id: "msg-ignored",
        event_type: "orchestrator.turn.started",
        severity: "info",
        payload: {
          message_preview: "people chatting nearby",
          stage: "context",
        },
      },
      {
        event_id: "evt-ignored-3",
        trace_id: "trace-ignored",
        span_id: "span-ignored-3",
        parent_span_id: "span-ignored-2",
        ts: 3,
        session_key: "session-ignored",
        session_id: "sess-ignored",
        platform: "napcat",
        chat_type: "group",
        chat_id: "chat-ignored",
        user_id: "user-ignored",
        message_id: "msg-ignored",
        event_type: "orchestrator.decision.parsed",
        severity: "info",
        payload: {
          ignore_message: true,
          reasoning_summary: "Not agent-related.",
          stage: "context",
        },
      },
      {
        event_id: "evt-ignored-4",
        trace_id: "trace-ignored",
        span_id: "span-ignored-4",
        parent_span_id: "span-ignored-3",
        ts: 4,
        session_key: "session-ignored",
        session_id: "sess-ignored",
        platform: "napcat",
        chat_type: "group",
        chat_id: "chat-ignored",
        user_id: "user-ignored",
        message_id: "msg-ignored",
        event_type: "orchestrator.turn.ignored",
        severity: "info",
        payload: {
          reason: "Not agent-related.",
          stage: "final",
        },
      },
    ],
  );

  return {
    ...buildControllerWithLongTrace(),
    traces: [group.trace],
    groups: [group],
    selectedTrace: group.trace,
    selectedTraceEvents: group.rawEvents,
    selectedGroup: group,
  };
}

function buildControllerWithToolCallRequest(): ObservabilityController {
  const group = mapTraceToLiveGroup(
    {
      trace_id: "trace-tools",
      session_key: "session-tools",
      session_id: "sess-tools",
      chat_type: "group",
      chat_id: "chat-tools",
      user_id: "user-tools",
      message_id: "msg-tools",
      started_at: 1,
      ended_at: 4,
      event_count: 6,
      status: "completed",
      latest_stage: "final",
      summary: { headline: "tool trace" },
    },
    [
      {
        event_id: "evt-tools-1",
        trace_id: "trace-tools",
        span_id: "span-tools-1",
        parent_span_id: "",
        ts: 1,
        session_key: "session-tools",
        session_id: "sess-tools",
        platform: "napcat",
        chat_type: "group",
        chat_id: "chat-tools",
        user_id: "user-tools",
        message_id: "msg-tools",
        event_type: "agent.model.requested",
        severity: "info",
        payload: {
          llm_request_id: "req-tools-1",
          model: "gpt-test",
          provider: "openai",
          request_body: "{\"messages\":[\"use a tool\"]}",
          stage: "model",
        },
      },
      {
        event_id: "evt-tools-2",
        trace_id: "trace-tools",
        span_id: "span-tools-2",
        parent_span_id: "span-tools-1",
        ts: 1.2,
        session_key: "session-tools",
        session_id: "sess-tools",
        platform: "napcat",
        chat_type: "group",
        chat_id: "chat-tools",
        user_id: "user-tools",
        message_id: "msg-tools",
        event_type: "agent.reasoning.delta",
        severity: "info",
        payload: {
          llm_request_id: "req-tools-1",
          content: "inspect file",
          sequence: 1,
          stage: "model",
        },
      },
      {
        event_id: "evt-tools-3",
        trace_id: "trace-tools",
        span_id: "span-tools-3",
        parent_span_id: "span-tools-1",
        ts: 2,
        session_key: "session-tools",
        session_id: "sess-tools",
        platform: "napcat",
        chat_type: "group",
        chat_id: "chat-tools",
        user_id: "user-tools",
        message_id: "msg-tools",
        event_type: "agent.tool.called",
        severity: "info",
        payload: {
          llm_request_id: "req-tools-1",
          tool_call_id: "call-tools-1",
          tool_name: "read_file",
          args_preview: "{\"path\":\"README.md\"}",
          stage: "tools",
        },
      },
      {
        event_id: "evt-tools-4",
        trace_id: "trace-tools",
        span_id: "span-tools-4",
        parent_span_id: "span-tools-3",
        ts: 2.4,
        session_key: "session-tools",
        session_id: "sess-tools",
        platform: "napcat",
        chat_type: "group",
        chat_id: "chat-tools",
        user_id: "user-tools",
        message_id: "msg-tools",
        event_type: "agent.tool.completed",
        severity: "info",
        payload: {
          llm_request_id: "req-tools-1",
          tool_call_id: "call-tools-1",
          tool_name: "read_file",
          result_preview: "file contents",
          stage: "tools",
        },
      },
      {
        event_id: "evt-tools-5",
        trace_id: "trace-tools",
        span_id: "span-tools-5",
        parent_span_id: "span-tools-1",
        ts: 3,
        session_key: "session-tools",
        session_id: "sess-tools",
        platform: "napcat",
        chat_type: "group",
        chat_id: "chat-tools",
        user_id: "user-tools",
        message_id: "msg-tools",
        event_type: "agent.response.delta",
        severity: "info",
        payload: {
          llm_request_id: "req-tools-1",
          content: "done",
          sequence: 1,
          stage: "model",
        },
      },
      {
        event_id: "evt-tools-6",
        trace_id: "trace-tools",
        span_id: "span-tools-6",
        parent_span_id: "span-tools-1",
        ts: 4,
        session_key: "session-tools",
        session_id: "sess-tools",
        platform: "napcat",
        chat_type: "group",
        chat_id: "chat-tools",
        user_id: "user-tools",
        message_id: "msg-tools",
        event_type: "agent.model.completed",
        severity: "info",
        payload: {
          llm_request_id: "req-tools-1",
          model: "gpt-test",
          provider: "openai",
          total_tokens: 40,
          duration_ms: 3000,
          stage: "model",
        },
      },
    ],
  );

  return {
    ...buildControllerWithLongTrace(),
    traces: [group.trace],
    groups: [group],
    selectedTrace: group.trace,
    selectedTraceEvents: group.rawEvents,
    selectedGroup: group,
  };
}

describe("TraceInspector rendering", () => {
  it("renders a primary overview summary and collapses secondary metadata", () => {
    const html = renderToStaticMarkup(
      <MemoryRouter>
        <TraceInspector controller={buildControllerWithLongTrace()} />
      </MemoryRouter>,
    );

    expect(html).toContain('data-overview-grid="true"');
    expect(html).toContain('data-overview-stat="trace_id"');
    expect(html).toContain('data-overview-stat="session"');
    expect(html).toContain('data-overview-stat="chat"');
    expect(html).toContain('data-overview-stat="llm_requests"');
    expect(html).not.toContain('data-overview-stat="status"');
    expect(html).not.toContain('data-overview-stat="user"');
    expect(html).toContain('data-overview-secondary="true"');
    expect(html).toContain("LLM Total");
    expect(html).toContain("cache_hit_rate=");
    expect(html).toContain('data-llm-breakdowns="true"');
    expect(html).toContain("cost");
    expect(html).toContain("Execution Stream");
  });

  it("renders timeline entries in a stream with status dots", () => {
    const html = renderToStaticMarkup(
      <MemoryRouter>
        <TraceInspector controller={buildControllerWithLongTrace()} />
      </MemoryRouter>,
    );

    expect(html).toContain('id="stream-item-');
    expect(html).toContain("bg-emerald-500");
    expect(html).toContain("expand");
    expect(html).toContain("gpt-test request");
  });

  it("expands running requests by default and shows pulse animation", () => {
    const controller = buildControllerWithLongTrace();
    const group = controller.groups[0];
    for (const lane of group.timeline.lanes) {
      for (const block of lane.requestBlocks) {
        (block as any).status = "running";
        (block as any).endedAt = null;
      }
    }

    const html = renderToStaticMarkup(
      <MemoryRouter>
        <TraceInspector controller={controller} />
      </MemoryRouter>,
    );

    expect(html).toContain("animate-ping");
    expect(html).toContain("bg-sky-400");
    expect(html).toContain("running");
    expect(html).toContain("collapse");
    expect(html).toContain("Full Request Content");
  });

  it("collapses completed requests by default", () => {
    const html = renderToStaticMarkup(
      <MemoryRouter>
        <TraceInspector controller={buildControllerWithLongTrace()} />
      </MemoryRouter>,
    );

    expect(html).toContain("completed");
    expect(html).not.toContain("Full Request Content");
    expect(html).toContain("expand");
  });

  it("shows event cards in the stream with details collapsed by default", () => {
    const html = renderToStaticMarkup(
      <MemoryRouter>
        <TraceInspector controller={buildControllerWithStandaloneMemoryEvent()} />
      </MemoryRouter>,
    );

    expect(html).toContain("Prefetched identity memory");
    // 默认折叠，不显示 TimelineEventCard 的内部内容
    expect(html).not.toContain("Debug Payload");
    expect(html).not.toContain("auto_injection");
    expect(html).not.toContain("Request Body");
    expect(html).not.toContain("Reasoning");
    expect(html).not.toContain("Response");
    expect(html).toContain("expand");
  });

  it("keeps plain context visible while hiding mixed payload parameter blocks", () => {
    const html = renderToStaticMarkup(
      <MemoryRouter>
        <TraceInspector controller={buildControllerWithMixedContextDebug()} />
      </MemoryRouter>,
    );

    expect(html).toContain("Context Debug");
    expect(html).toContain("memory_prefetch_fenced");
    expect(html).toContain("&lt;memory-context&gt;Alice&lt;/memory-context&gt;");
    expect(html).toContain("memory_prefetch_params_by_provider");
    expect(html).not.toContain("&quot;query_mode&quot;: &quot;mix&quot;");
  });

  it("shows ignored trace events in the stream without lane sections", () => {
    const html = renderToStaticMarkup(
      <MemoryRouter>
        <TraceInspector controller={buildControllerWithIgnoredTrace()} />
      </MemoryRouter>,
    );

    expect(html).toContain("ignored");
    expect(html).toContain("message.received");
    expect(html).toContain("orchestrator.turn.ignored");
    expect(html).not.toContain('data-inspector-section-forced-open="true"');
  });

  it("shows tool call requests in the stream", () => {
    const html = renderToStaticMarkup(
      <MemoryRouter>
        <TraceInspector controller={buildControllerWithToolCallRequest()} />
      </MemoryRouter>,
    );

    expect(html).toContain("gpt-test request");
    expect(html).toContain("expand");
  });
});
