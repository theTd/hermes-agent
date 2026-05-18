import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";

import { TraceGroupCard } from "../TraceGroupCard";
import type { LiveTraceGroup } from "../../types";

function buildGroup(): LiveTraceGroup {
  return {
    traceId: "trace-1",
    endedAt: 2,
    headline: `{"action":"reply","payload":{"text":"${"A".repeat(220)}"}}`,
    headlineSource: "response",
    status: "completed",
    latestStage: "final",
    startedAt: 1,
    lastEventAt: 2,
    durationMs: 1000,
    eventCount: 3,
    errorCount: 0,
    toolCallCount: 0,
    model: "test-model",
    activeStream: false,
    toolCalls: [],
    streamChunks: [],
    stages: [],
    rawEvents: [{
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
      payload: {
        text_preview: `user asks ${"Q".repeat(220)}`,
      },
    }],
    trace: {
      trace_id: "trace-1",
      session_key: "session-1",
      session_id: "sess-1",
      chat_type: "group",
      chat_id: "chat-1",
      user_id: "user-1",
      message_id: "msg-1",
      started_at: 1,
      ended_at: 2,
      event_count: 3,
      status: "completed",
      latest_stage: "final",
      summary: {},
    },
    timeline: {
      traceId: "trace-1",
      sessionKey: "session-1",
      headline: "headline",
      status: "completed",
      lastEventAt: 2,
      requestCount: 1,
      laneCount: 1,
      lanes: [{
        key: "main_agent",
        title: "Main Agent",
        agentScope: "main_agent",
        childId: "",
        model: "test-model",
        provider: "test-provider",
        startedAt: 1,
        endedAt: 2,
        status: "completed",
        requestBlocks: [],
        timelineEntries: [{
          id: "entry-1",
          kind: "event",
          ts: 2,
          startedAt: 2,
          endedAt: 2,
          laneKey: "main_agent",
          llmRequestId: null,
          title: "agent.response.final",
          summary: `{"action":"spawn","payload":{"goal":"${"B".repeat(220)}"}}`,
          status: "completed",
          durationMs: null,
          payloadPreview: "",
          event: null,
        }],
        rawEvents: [],
      }],
    },
    mainOrchestrator: null,
    childTasks: [],
    childDetailsById: {},
    activeChildren: [],
    recentChildren: [],
  };
}

describe("TraceGroupCard", () => {
  it("keeps left-rail cards compact while preserving full text in title attributes", () => {
    const group = buildGroup();
    const html = renderToStaticMarkup(
      <TraceGroupCard
        group={group}
        selected={false}
        onSelect={() => {}}
      />,
    );

    expect(html).toContain("title=\"user asks");
    expect(html).not.toContain("title=\"{&quot;action&quot;:&quot;reply&quot;");
    expect(html).toContain("title=\"{&quot;action&quot;:&quot;spawn&quot;");
    expect(html).toContain("truncate");
  });
});
