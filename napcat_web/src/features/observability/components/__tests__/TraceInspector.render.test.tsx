import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
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
      },
      runtime: {},
      stats: null,
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

function buildControllerWithMainOrchestratorThinking(): ObservabilityController {
  const group = mapTraceToLiveGroup(
    {
      trace_id: "trace-orchestrator",
      session_key: "session-orchestrator",
      session_id: "sess-orchestrator",
      chat_type: "group",
      chat_id: "chat-orchestrator",
      user_id: "user-orchestrator",
      message_id: "msg-orchestrator",
      started_at: 1,
      ended_at: null,
      event_count: 5,
      status: "in_progress",
      latest_stage: "model",
      summary: { headline: "orchestrator trace" },
    },
    [
      {
        event_id: "evt-orch-1",
        trace_id: "trace-orchestrator",
        span_id: "span-orch-1",
        parent_span_id: "",
        ts: 1,
        session_key: "session-orchestrator",
        session_id: "sess-orchestrator",
        platform: "napcat",
        chat_type: "group",
        chat_id: "chat-orchestrator",
        user_id: "user-orchestrator",
        message_id: "msg-orchestrator",
        event_type: "orchestrator.turn.started",
        severity: "info",
        payload: {
          message_preview: "route this",
          stage: "context",
        },
      },
      {
        event_id: "evt-orch-2",
        trace_id: "trace-orchestrator",
        span_id: "span-orch-2",
        parent_span_id: "span-orch-1",
        ts: 1.1,
        session_key: "session-orchestrator",
        session_id: "sess-orchestrator",
        platform: "napcat",
        chat_type: "group",
        chat_id: "chat-orchestrator",
        user_id: "user-orchestrator",
        message_id: "msg-orchestrator",
        event_type: "agent.model.requested",
        severity: "info",
        payload: {
          llm_request_id: "orch-req-1",
          model: "router-model",
          provider: "openai",
          agent_scope: "main_orchestrator",
          request_body: "{\"messages\":[\"route this\"]}",
          stage: "model",
        },
      },
      {
        event_id: "evt-orch-3",
        trace_id: "trace-orchestrator",
        span_id: "span-orch-3",
        parent_span_id: "span-orch-2",
        ts: 1.2,
        session_key: "session-orchestrator",
        session_id: "sess-orchestrator",
        platform: "napcat",
        chat_type: "group",
        chat_id: "chat-orchestrator",
        user_id: "user-orchestrator",
        message_id: "msg-orchestrator",
        event_type: "agent.reasoning.delta",
        severity: "info",
        payload: {
          llm_request_id: "orch-req-1",
          agent_scope: "main_orchestrator",
          content: "first pass reasoning",
          sequence: 1,
          stage: "model",
        },
      },
      {
        event_id: "evt-orch-4",
        trace_id: "trace-orchestrator",
        span_id: "span-orch-4",
        parent_span_id: "span-orch-2",
        ts: 1.3,
        session_key: "session-orchestrator",
        session_id: "sess-orchestrator",
        platform: "napcat",
        chat_type: "group",
        chat_id: "chat-orchestrator",
        user_id: "user-orchestrator",
        message_id: "msg-orchestrator",
        event_type: "agent.reasoning.delta",
        severity: "info",
        payload: {
          llm_request_id: "orch-req-1",
          agent_scope: "main_orchestrator",
          content: "first pass reasoning with more detail",
          sequence: 2,
          stage: "model",
        },
      },
      {
        event_id: "evt-orch-5",
        trace_id: "trace-orchestrator",
        span_id: "span-orch-5",
        parent_span_id: "span-orch-2",
        ts: 1.4,
        session_key: "session-orchestrator",
        session_id: "sess-orchestrator",
        platform: "napcat",
        chat_type: "group",
        chat_id: "chat-orchestrator",
        user_id: "user-orchestrator",
        message_id: "msg-orchestrator",
        event_type: "orchestrator.decision.parsed",
        severity: "info",
        payload: {
          reasoning_summary: "route it deeper",
          stage: "context",
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
      },
      runtime: {},
      stats: null,
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
      <TraceInspector controller={buildControllerWithLongTrace()} />,
    );

    expect(html).toContain('data-overview-grid="true"');
    expect(html).toContain('data-overview-stat="trace_id"');
    expect(html).toContain('data-overview-stat="session"');
    expect(html).toContain('data-overview-stat="chat"');
    expect(html).toContain('data-overview-stat="llm_requests"');
    expect(html).not.toContain('data-overview-stat="status"');
    expect(html).not.toContain('data-overview-stat="user"');
    expect(html).toContain('data-overview-secondary="true"');
    expect(html).toContain("<details");
    expect(html).toContain("More Trace Metadata");
    expect(html).toContain("grid-cols-1 gap-2 sm:grid-cols-2 xl:grid-cols-4");
    expect(html).toContain("LLM Total");
    expect(html).toContain("cost");
  });

  it("keeps main orchestrator thinking always visible and untrimmed", () => {
    const html = renderToStaticMarkup(
      <TraceInspector controller={buildControllerWithMainOrchestratorThinking()} />,
    );

    expect(html).toContain("Main Orchestrator");
    expect(html).toContain('data-inspector-section-forced-open="true"');
    expect(html).toContain('data-inspector-section-collapsible="false"');
    expect(html).toContain('data-main-orchestrator-thinking="true"');
    expect(html).toContain("Main Orchestrator Thinking");
    expect(html).toContain("first pass reasoning with more detail");
    expect(html).not.toContain("always visible</div></div><pre class=\"mt-3 max-w-full whitespace-pre-wrap break-all rounded-xl border border-border/20 bg-background/35 p-3 text-xs font-mono-ui [overflow-wrap:anywhere] max-h-40 overflow-hidden");
    expect(html).not.toContain("aria-expanded");
    expect(html).not.toContain('data-request-preview="thinking"');
  });

  it("hides request payload bodies by default while keeping thinking and response visible", () => {
    const html = renderToStaticMarkup(
      <TraceInspector controller={buildControllerWithLongTrace()} />,
    );

    expect(html).toContain('data-request-card="true"');
    expect(html).toContain('data-request-primary-stats="true"');
    expect(html).toContain('data-request-stat="duration"');
    expect(html).toContain('data-request-stat="total"');
    expect(html).toContain('data-request-metadata="true"');
    expect(html).toContain("More Request Metadata");
    expect(html).toContain('data-request-content="true"');
    expect(html).toContain('data-request-content-default-open="false"');
    expect(html).toContain("Full Request Content");
    expect(html).toContain('data-request-preview-summary="true"');
    expect(html).toContain('data-request-preview="thinking"');
    expect(html).toContain('data-request-preview="response"');
    expect(html).toContain('data-thinking-steps="true"');
    expect(html).toContain('data-thinking-step="1"');
    expect(html).toContain("Thinking · 1 step");
    expect(html).toContain("max-h-40 overflow-hidden");
    expect(html).toContain("Request Body");
    expect(html).toContain("Response");
    expect(html).toContain("expand");
    expect(html).toContain("THINK THINK THINK THINK");
    expect(html).toContain("RESP RESP RESP RESP");
    expect(html).not.toContain("grid-cols-[minmax(0,9rem)_minmax(0,1fr)]");
  });

  it("defaults request content open when the request contains tool calls", () => {
    const html = renderToStaticMarkup(
      <TraceInspector controller={buildControllerWithToolCallRequest()} />,
    );

    expect(html).toContain('data-request-content-default-open="true"');
    expect(html).toContain("Full Request Content");
    expect(html).toContain("Tools");
    expect(html).toContain("read_file");
    expect(html).toContain("file contents");
  });

  it("renders standalone memory activity as an event card with hidden debug payload", () => {
    const html = renderToStaticMarkup(
      <TraceInspector controller={buildControllerWithStandaloneMemoryEvent()} />,
    );

    expect(html).toContain("Prefetched identity memory");
    expect(html).toContain("Debug Payload");
    expect(html).not.toContain("auto_injection");
    expect(html).not.toContain("Request Body");
    expect(html).not.toContain("Thinking");
    expect(html).not.toContain("Response");
  });

  it("keeps plain context visible while hiding mixed payload parameter blocks", () => {
    const html = renderToStaticMarkup(
      <TraceInspector controller={buildControllerWithMixedContextDebug()} />,
    );

    expect(html).toContain("Context Debug");
    expect(html).toContain("memory_prefetch_fenced");
    expect(html).toContain("&lt;memory-context&gt;Alice&lt;/memory-context&gt;");
    expect(html).toContain("memory_prefetch_params_by_provider");
    expect(html).not.toContain("&quot;query_mode&quot;: &quot;mix&quot;");
  });

  it("hides the main-agent lane for ignored orchestrator-only traces", () => {
    const html = renderToStaticMarkup(
      <TraceInspector controller={buildControllerWithIgnoredTrace()} />,
    );

    expect(html).toContain("ignored");
    expect(html).toContain("Main Orchestrator");
    expect(html).toContain('data-inspector-section-forced-open="true"');
    expect(html).toContain("message.received");
    expect(html).toContain("orchestrator.turn.started");
    expect(html).not.toContain("Main Agent");
  });
});
