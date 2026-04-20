import { describe, expect, it } from "vitest";
import type { NapcatEvent, NapcatTrace } from "@/lib/napcat-api";
import { mapTraceToLiveGroup } from "../stage-mapper";

function event(overrides: Partial<NapcatEvent>): NapcatEvent {
  return {
    event_id: overrides.event_id ?? Math.random().toString(16).slice(2),
    trace_id: overrides.trace_id ?? "trace_1",
    span_id: overrides.span_id ?? "span_1",
    parent_span_id: overrides.parent_span_id ?? "",
    ts: overrides.ts ?? 1,
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

function trace(overrides: Partial<NapcatTrace> = {}): NapcatTrace {
  return {
    trace_id: overrides.trace_id ?? "trace_1",
    session_key: overrides.session_key ?? "session_1",
    session_id: overrides.session_id ?? "sess_1",
    chat_type: overrides.chat_type ?? "group",
    chat_id: overrides.chat_id ?? "chat_1",
    user_id: overrides.user_id ?? "user_1",
    message_id: overrides.message_id ?? "msg_1",
    started_at: overrides.started_at ?? 1,
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

describe("mapTraceToLiveGroup", () => {
  it("aggregates tool lifecycle by tool_call_id", () => {
    const group = mapTraceToLiveGroup(
      trace({ event_count: 3 }),
      [
        event({ ts: 1, event_type: "agent.tool.called", payload: { tool_name: "read_file", tool_call_id: "call_1", args_preview: '{"path":"a"}', stage: "tools" } }),
        event({ ts: 2, event_type: "agent.tool.completed", payload: { tool_name: "read_file", tool_call_id: "call_1", result_preview: "ok", stage: "tools" } }),
        event({ ts: 3, event_type: "agent.response.final", payload: { response_preview: "done", stage: "final" } }),
      ],
    );

    expect(group.toolCalls).toHaveLength(1);
    expect(group.toolCalls[0]?.status).toBe("completed");
    expect(group.toolCallCount).toBe(1);
    expect(group.headline).toBe("done");
    expect(group.latestStage).toBe("final");
  });

  it("orders stream chunks by ts and sequence and prioritizes error headline", () => {
    const group = mapTraceToLiveGroup(
      trace({ status: "error", event_count: 4 }),
      [
        event({ ts: 1, event_type: "message.received", payload: { text_preview: "hello", stage: "inbound" } }),
        event({ ts: 2, event_type: "agent.response.delta", payload: { content: "world", sequence: 2, stage: "model" } }),
        event({ ts: 2, event_type: "agent.reasoning.delta", payload: { content: "think", sequence: 1, stage: "model" } }),
        event({ ts: 3, event_type: "error.raised", severity: "error", payload: { error_message: "boom", stage: "error" } }),
      ],
    );

    expect(group.streamChunks.map((chunk) => chunk.sequence)).toEqual([1, 2]);
    expect(group.headline).toBe("boom");
    expect(group.latestStage).toBe("error");
    expect(group.errorCount).toBe(1);
  });

  it("reconstructs cumulative stream chunk content from delta-only events", () => {
    const group = mapTraceToLiveGroup(
      trace({ event_count: 4, status: "completed", latest_stage: "final" }),
      [
        event({ ts: 1, event_type: "agent.reasoning.delta", payload: { delta: "first", sequence: 1, llm_request_id: "req-1", stage: "model" } }),
        event({ ts: 2, event_type: "agent.reasoning.delta", payload: { delta: " pass", sequence: 2, llm_request_id: "req-1", stage: "model" } }),
        event({ ts: 3, event_type: "agent.response.delta", payload: { delta: "hello", sequence: 1, llm_request_id: "req-1", stage: "model" } }),
        event({ ts: 4, event_type: "agent.response.delta", payload: { delta: " world", sequence: 2, llm_request_id: "req-1", stage: "model" } }),
      ],
    );

    expect(group.streamChunks).toContainEqual(
      expect.objectContaining({
        kind: "thinking",
        sequence: 2,
        content: "first pass",
      }),
    );
    expect(group.streamChunks).toContainEqual(
      expect.objectContaining({
        kind: "response",
        sequence: 2,
        content: "hello world",
      }),
    );
  });

  it("uses a trace-created placeholder headline before message preview arrives", () => {
    const group = mapTraceToLiveGroup(
      trace({
        chat_type: "private",
        chat_id: "chat_early",
        user_id: "user_early",
        message_id: "msg_early",
        event_count: 1,
      }),
      [
        event({
          ts: 1,
          event_type: "trace.created",
          payload: { message_type: "private", stage: "inbound" },
        }),
      ],
    );

    expect(group.headline).toBe("Inbound message received");
    expect(group.headlineSource).toBe("trace");
  });

  it("pairs tool lifecycle without explicit tool_call_id", () => {
    const group = mapTraceToLiveGroup(
      trace({ event_count: 3 }),
      [
        event({ ts: 1, event_type: "agent.tool.called", payload: { tool_name: "read_file", args_preview: '{"path":"a"}', stage: "tools" } }),
        event({ ts: 2, event_type: "agent.tool.completed", payload: { tool_name: "read_file", result_preview: "ok", stage: "tools" } }),
        event({ ts: 3, event_type: "agent.response.final", payload: { response_preview: "done", stage: "final" } }),
      ],
    );

    expect(group.toolCalls).toHaveLength(1);
    expect(group.toolCalls[0]?.status).toBe("completed");
    expect(group.toolCalls[0]?.resultPreview).toBe("ok");
  });

  it("derives main orchestrator and child task state separately", () => {
    const group = mapTraceToLiveGroup(
      trace({ event_count: 7, status: "completed", latest_stage: "final" }),
      [
        event({ ts: 1, event_type: "orchestrator.turn.started", payload: { message_preview: "please analyze", stage: "context" } }),
        event({
          ts: 1.5,
          event_type: "agent.model.requested",
          payload: {
            model: "main-model",
            request_body: '{"messages":["main"]}',
            agent_scope: "main_orchestrator",
            stage: "model",
          },
        }),
        event({
          ts: 1.6,
          event_type: "agent.reasoning.delta",
          payload: { content: "main thinking", sequence: 1, agent_scope: "main_orchestrator", stage: "model" },
        }),
        event({
          ts: 2,
          event_type: "orchestrator.decision.parsed",
          payload: {
            stage: "context",
            respond_now: true,
            ignore_message: false,
            spawn_count: 1,
            cancel_count: 0,
            used_fallback: false,
            reasoning_summary: "Heavy task.",
            raw_response: '{"respond_now":true}',
          },
        }),
        event({
          ts: 3,
          event_type: "orchestrator.child.spawned",
          payload: {
            child_id: "child_1",
            goal: "analyze repo",
            request_kind: "analysis",
            status: "queued",
            runtime_session_id: "runtime-child-1",
            runtime_session_key: "agent:main:napcat:dm:chat-1::child_1",
            stage: "tools",
          },
        }),
        event({
          ts: 3.2,
          event_type: "agent.model.requested",
          payload: {
            child_id: "child_1",
            agent_scope: "orchestrator_child",
            model: "child-model",
            request_body: '{"messages":["child"]}',
            stage: "model",
          },
        }),
        event({
          ts: 3.5,
          event_type: "agent.response.delta",
          payload: { content: "child response", sequence: 1, agent_scope: "orchestrator_child", child_id: "child_1", stage: "model" },
        }),
        event({
          ts: 4,
          event_type: "orchestrator.child.completed",
          payload: { child_id: "child_1", goal: "analyze repo", result_preview: "repo analysis finished", summary: "repo analysis finished", stage: "final" },
        }),
      ],
    );

    expect(group.mainOrchestrator?.status).toBe("spawned");
    expect(group.mainOrchestrator?.spawnCount).toBe(1);
    expect(group.mainOrchestrator?.rawResponse).toBe('{"respond_now":true}');
    expect(group.childTasks).toHaveLength(1);
    expect(group.childTasks[0]?.status).toBe("completed");
    expect(group.childTasks[0]?.summary).toBe("repo analysis finished");
    expect(group.childTasks[0]?.runtimeSessionId).toBe("runtime-child-1");
    expect(group.childDetailsById.child_1?.model).toBe("child-model");
    expect(group.childDetailsById.child_1?.rawEvents).toHaveLength(4);
    expect(group.streamChunks).toEqual([
      expect.objectContaining({ kind: "thinking", agentScope: "main_orchestrator", content: "main thinking" }),
      expect.objectContaining({ kind: "response", agentScope: "orchestrator_child", childId: "child_1", content: "child response" }),
    ]);
    expect(group.headline).toBe("repo analysis finished");
    expect(group.headlineSource).toBe("child_result");
    expect(group.timeline.lanes.map((lane) => lane.key)).toEqual(["main_orchestrator", "child:child_1"]);
  });

  it("keeps ignored orchestrator turns out of the main-agent lane", () => {
    const group = mapTraceToLiveGroup(
      trace({ event_count: 4, status: "ignored", latest_stage: "final" }),
      [
        event({ ts: 1, event_type: "message.received", payload: { text_preview: "side chat", stage: "inbound" } }),
        event({ ts: 2, event_type: "orchestrator.turn.started", payload: { message_preview: "side chat", stage: "context" } }),
        event({ ts: 3, event_type: "orchestrator.decision.parsed", payload: { ignore_message: true, reasoning_summary: "Not addressed to the agent.", stage: "context" } }),
        event({ ts: 4, event_type: "orchestrator.turn.ignored", payload: { reason: "Not addressed to the agent.", stage: "final" } }),
      ],
    );

    expect(group.timeline.laneCount).toBe(1);
    expect(group.timeline.lanes[0]?.key).toBe("main_orchestrator");
    expect(group.timeline.lanes[0]?.status).toBe("ignored");
    expect(group.timeline.lanes[0]?.timelineEntries).toContainEqual(
      expect.objectContaining({
        title: "orchestrator.turn.ignored",
        summary: "Not addressed to the agent.",
      }),
    );
  });

  it("builds separate lane request blocks by llm_request_id", () => {
    const group = mapTraceToLiveGroup(
      trace({ event_count: 8, status: "completed", latest_stage: "final" }),
      [
        event({ ts: 1, event_type: "message.received", payload: { text_preview: "hello", stage: "inbound" } }),
        event({
          ts: 2,
          event_type: "agent.model.requested",
          payload: {
            llm_request_id: "req-main",
            model: "main-model",
            provider: "openai",
            request_body: '{"messages":["main"]}',
            stage: "model",
          },
        }),
        event({
          ts: 2.2,
          event_type: "agent.response.delta",
          payload: {
            llm_request_id: "req-main",
            content: "main response",
            sequence: 1,
            stage: "model",
          },
        }),
        event({
          ts: 2.5,
          event_type: "agent.model.completed",
          payload: {
            llm_request_id: "req-main",
            model: "main-model",
            provider: "openai",
            output_tokens: 11,
            total_tokens: 22,
            duration_ms: 500,
            stage: "model",
          },
        }),
        event({
          ts: 3,
          event_type: "agent.model.requested",
          payload: {
            llm_request_id: "req-child",
            child_id: "child_1",
            agent_scope: "orchestrator_child",
            model: "child-model",
            provider: "anthropic",
            request_body: '{"messages":["child"]}',
            stage: "model",
          },
        }),
        event({
          ts: 3.2,
          event_type: "agent.reasoning.delta",
          payload: {
            llm_request_id: "req-child",
            child_id: "child_1",
            agent_scope: "orchestrator_child",
            content: "child thinking",
            sequence: 1,
            stage: "model",
          },
        }),
        event({
          ts: 3.5,
          event_type: "agent.model.completed",
          payload: {
            llm_request_id: "req-child",
            child_id: "child_1",
            agent_scope: "orchestrator_child",
            model: "child-model",
            provider: "anthropic",
            output_tokens: 7,
            total_tokens: 15,
            duration_ms: 350,
            stage: "model",
          },
        }),
      ],
    );

    expect(group.timeline.laneCount).toBe(2);
    expect(group.timeline.requestCount).toBe(2);
    expect(group.timeline.lanes[0]?.key).toBe("main_agent");
    expect(group.timeline.lanes[0]?.requestBlocks[0]).toEqual(
      expect.objectContaining({
        id: "req-main",
        model: "main-model",
        provider: "openai",
        responseText: "main response",
        ttftMs: 200,
      }),
    );
    expect(group.timeline.lanes[1]?.key).toBe("child:child_1");
    expect(group.timeline.lanes[1]?.requestBlocks[0]).toEqual(
      expect.objectContaining({
        id: "req-child",
        model: "child-model",
        provider: "anthropic",
        thinkingFullText: "child thinking",
        thinkingText: "child thinking",
        thinkingStepCount: 1,
        ttftMs: 200,
      }),
    );
    expect(group.timeline.lanes[1]?.requestBlocks[0]?.thinkingSteps).toEqual([
      expect.objectContaining({
        sequence: 1,
        source: "suffix",
        text: "child thinking",
      }),
    ]);
  });

  it("keeps cumulative reasoning in a single thinking step per request", () => {
    const group = mapTraceToLiveGroup(
      trace({ event_count: 4, status: "completed", latest_stage: "model" }),
      [
        event({
          ts: 1,
          event_type: "agent.model.requested",
          payload: {
            llm_request_id: "req-steps",
            model: "router-model",
            request_body: '{"messages":["route"]}',
            stage: "model",
          },
        }),
        event({
          ts: 1.1,
          event_type: "agent.reasoning.delta",
          payload: {
            llm_request_id: "req-steps",
            content: "first pass reasoning",
            sequence: 1,
            stage: "model",
          },
        }),
        event({
          ts: 1.2,
          event_type: "agent.reasoning.delta",
          payload: {
            llm_request_id: "req-steps",
            content: "first pass reasoning with more detail",
            sequence: 2,
            stage: "model",
          },
        }),
        event({
          ts: 1.3,
          event_type: "agent.model.completed",
          payload: {
            llm_request_id: "req-steps",
            total_tokens: 17,
            duration_ms: 300,
            stage: "model",
          },
        }),
      ],
    );

    const block = group.timeline.lanes[0]?.requestBlocks[0];
    expect(block?.thinkingFullText).toBe("first pass reasoning with more detail");
    expect(block?.thinkingText).toBe("first pass reasoning with more detail");
    expect(block?.thinkingStepCount).toBe(1);
    expect(block?.thinkingSteps).toEqual([
      expect.objectContaining({
        sequence: 2,
        source: "suffix",
        text: "first pass reasoning with more detail",
      }),
    ]);
  });

  it("replaces the single thinking step when reasoning content resets", () => {
    const group = mapTraceToLiveGroup(
      trace({ event_count: 5, status: "completed", latest_stage: "model" }),
      [
        event({
          ts: 1,
          event_type: "agent.model.requested",
          payload: {
            llm_request_id: "req-reset",
            model: "router-model",
            request_body: '{"messages":["route"]}',
            stage: "model",
          },
        }),
        event({
          ts: 1.1,
          event_type: "agent.reasoning.delta",
          payload: {
            llm_request_id: "req-reset",
            content: "alpha",
            delta: "alpha",
            sequence: 1,
            stage: "model",
          },
        }),
        event({
          ts: 1.2,
          event_type: "agent.reasoning.delta",
          payload: {
            llm_request_id: "req-reset",
            content: "alpha",
            delta: "alpha",
            sequence: 2,
            stage: "model",
          },
        }),
        event({
          ts: 1.3,
          event_type: "agent.reasoning.delta",
          payload: {
            llm_request_id: "req-reset",
            content: "beta plan",
            delta: " plan",
            sequence: 3,
            stage: "model",
          },
        }),
        event({
          ts: 1.4,
          event_type: "agent.model.completed",
          payload: {
            llm_request_id: "req-reset",
            total_tokens: 17,
            duration_ms: 300,
            stage: "model",
          },
        }),
      ],
    );

    const block = group.timeline.lanes[0]?.requestBlocks[0];
    expect(block?.thinkingStepCount).toBe(1);
    expect(block?.thinkingSteps).toEqual([
      expect.objectContaining({
        sequence: 3,
        source: "reset",
        text: "beta plan",
      }),
    ]);
    expect(block?.thinkingFullText).toBe("beta plan");
  });

  it("falls back to legacy request grouping without llm_request_id", () => {
    const group = mapTraceToLiveGroup(
      trace({ event_count: 4, status: "completed", latest_stage: "final" }),
      [
        event({
          ts: 1,
          event_type: "agent.model.requested",
          payload: { model: "model-a", request_body: '{"a":1}', stage: "model" },
        }),
        event({
          ts: 1.2,
          event_type: "agent.model.completed",
          payload: { model: "model-a", total_tokens: 10, duration_ms: 200, stage: "model" },
        }),
        event({
          ts: 2,
          event_type: "agent.model.requested",
          payload: { model: "model-b", request_body: '{"b":1}', stage: "model" },
        }),
        event({
          ts: 2.2,
          event_type: "agent.model.completed",
          payload: { model: "model-b", total_tokens: 12, duration_ms: 300, stage: "model" },
        }),
      ],
    );

    expect(group.timeline.requestCount).toBe(2);
    expect(group.timeline.lanes[0]?.requestBlocks.map((block) => block.correlationMode)).toEqual([
      "legacy",
      "legacy",
    ]);
    expect(group.timeline.lanes[0]?.requestBlocks.map((block) => block.model)).toEqual([
      "model-a",
      "model-b",
    ]);
  });

  it("uses total request duration as ttft for completed non-streaming requests without deltas", () => {
    const group = mapTraceToLiveGroup(
      trace({ event_count: 2, status: "completed", latest_stage: "final" }),
      [
        event({
          ts: 1,
          event_type: "agent.model.requested",
          payload: {
            llm_request_id: "req-sync",
            model: "model-sync",
            streaming: false,
            request_body: '{"messages":["sync"]}',
            stage: "model",
          },
        }),
        event({
          ts: 1.4,
          event_type: "agent.model.completed",
          payload: {
            llm_request_id: "req-sync",
            model: "model-sync",
            duration_ms: 400,
            stage: "model",
          },
        }),
      ],
    );

    expect(group.timeline.lanes[0]?.requestBlocks[0]).toEqual(
      expect.objectContaining({
        id: "req-sync",
        durationMs: 400,
        ttftMs: 400,
      }),
    );
  });

  it("keeps unscoped memory activity as a standalone timeline event", () => {
    const group = mapTraceToLiveGroup(
      trace({ event_count: 1, status: "completed", latest_stage: "memory_skill_routing" }),
      [
        event({
          ts: 1,
          event_type: "agent.memory.used",
          payload: {
            source: "auto_injection",
            summary: "Prefetched identity memory",
            stage: "memory_skill_routing",
          },
        }),
      ],
    );

    expect(group.timeline.requestCount).toBe(0);
    expect(group.timeline.lanes[0]?.requestBlocks).toEqual([]);
    expect(group.timeline.lanes[0]?.timelineEntries).toContainEqual(
      expect.objectContaining({
        kind: "memory",
        llmRequestId: null,
        summary: "Prefetched identity memory",
      }),
    );
  });

  it("keeps request-local memory activity attached to the current legacy request block", () => {
    const group = mapTraceToLiveGroup(
      trace({ event_count: 3, status: "completed", latest_stage: "final" }),
      [
        event({
          ts: 1,
          event_type: "agent.model.requested",
          payload: { model: "model-a", request_body: '{"a":1}', stage: "model" },
        }),
        event({
          ts: 1.1,
          event_type: "agent.memory.used",
          payload: {
            source: "auto_injection",
            summary: "Injected memory into active request",
            stage: "memory_skill_routing",
          },
        }),
        event({
          ts: 1.2,
          event_type: "agent.model.completed",
          payload: { model: "model-a", total_tokens: 10, duration_ms: 200, stage: "model" },
        }),
      ],
    );

    expect(group.timeline.requestCount).toBe(1);
    expect(group.timeline.lanes[0]?.requestBlocks).toHaveLength(1);
    expect(group.timeline.lanes[0]?.requestBlocks[0]?.memoryEvents).toHaveLength(1);
    expect(group.timeline.lanes[0]?.timelineEntries).not.toContainEqual(
      expect.objectContaining({
        kind: "memory",
        summary: "Injected memory into active request",
      }),
    );
  });

  it("keeps activeStream true when later streaming continues after an earlier child terminal event", () => {
    const group = mapTraceToLiveGroup(
      trace({ event_count: 4, status: "in_progress", latest_stage: "model", active_stream: false }),
      [
        event({ ts: 1, event_type: "agent.response.delta", payload: { content: "first", sequence: 1, stage: "model" } }),
        event({ ts: 2, event_type: "orchestrator.child.completed", payload: { child_id: "child_1", stage: "final" } }),
        event({ ts: 3, event_type: "agent.response.delta", payload: { content: "second", sequence: 2, stage: "model" } }),
      ],
    );

    expect(group.activeStream).toBe(true);
  });

  it("keeps structured summaries and payload previews untrimmed", () => {
    const structured = JSON.stringify({
      action: "reply",
      payload: {
        text: "A".repeat(320),
      },
    });

    const group = mapTraceToLiveGroup(
      trace({ event_count: 2, status: "completed", latest_stage: "final" }),
      [
        event({
          ts: 1,
          event_type: "agent.response.final",
          payload: {
            response_preview: structured,
            stage: "final",
          },
        }),
        event({
          ts: 2,
          event_type: "orchestrator.decision.parsed",
          payload: {
            raw_response: structured,
            reasoning_summary: structured,
            stage: "context",
          },
        }),
      ],
    );

    const finalEntry = group.timeline.lanes[0]?.timelineEntries.find((entry) => entry.title === "agent.response.final");
    expect(finalEntry?.summary).toBe(structured);
    expect(finalEntry?.payloadPreview).toContain("A".repeat(320));
    expect(finalEntry?.payloadPreview).not.toContain("…");
  });
});
