import { describe, expect, it } from "vitest";
import { buildTraceDetailViewModel } from "../detail-view";
import { mapTraceToLiveGroup } from "../../stage-mapper";

describe("detail view model", () => {
  it("maps lane request blocks and overview entries for the detail panel", () => {
    const group = mapTraceToLiveGroup(
      {
        trace_id: "trace-1",
        session_key: "session-1",
        session_id: "session-1-id",
        chat_type: "group",
        chat_id: "chat-1",
        user_id: "user-1",
        message_id: "msg-1",
        started_at: 1,
        ended_at: 3,
        event_count: 4,
        status: "completed",
        latest_stage: "final",
        summary: { headline: "request trace" },
      },
      [
        {
          event_id: "evt-1",
          trace_id: "trace-1",
          span_id: "span-1",
          parent_span_id: "",
          ts: 1,
          session_key: "session-1",
          session_id: "session-1-id",
          platform: "napcat",
          chat_type: "group",
          chat_id: "chat-1",
          user_id: "user-1",
          message_id: "msg-1",
          event_type: "message.received",
          severity: "info",
          payload: { text_preview: "hello", stage: "inbound" },
        },
        {
          event_id: "evt-2",
          trace_id: "trace-1",
          span_id: "span-2",
          parent_span_id: "span-1",
          ts: 2,
          session_key: "session-1",
          session_id: "session-1-id",
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
            request_body: "{\"messages\":[\"hello\"]}",
            stage: "model",
          },
        },
        {
          event_id: "evt-3",
          trace_id: "trace-1",
          span_id: "span-3",
          parent_span_id: "span-2",
          ts: 2.5,
          session_key: "session-1",
          session_id: "session-1-id",
          platform: "napcat",
          chat_type: "group",
          chat_id: "chat-1",
          user_id: "user-1",
          message_id: "msg-1",
          event_type: "agent.response.delta",
          severity: "info",
          payload: {
            llm_request_id: "req-1",
            content: "response",
            sequence: 1,
            stage: "model",
          },
        },
        {
          event_id: "evt-4",
          trace_id: "trace-1",
          span_id: "span-4",
          parent_span_id: "span-2",
          ts: 3,
          session_key: "session-1",
          session_id: "session-1-id",
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
            total_tokens: 22,
            output_tokens: 11,
            duration_ms: 1000,
            stage: "model",
          },
        },
      ],
    );

    const viewModel = buildTraceDetailViewModel(group);

    expect(viewModel.primaryOverviewEntries).toEqual([
      { label: "trace_id", value: "trace-1" },
      { label: "session", value: "session-1" },
      { label: "chat", value: "group:chat-1" },
      { label: "llm_requests", value: "1" },
    ]);
    expect(viewModel.secondaryOverviewEntries).toEqual([
      { label: "status", value: "completed" },
      { label: "user", value: "user-1" },
      { label: "message", value: "msg-1" },
      { label: "lanes", value: "1" },
    ]);
    expect(viewModel.lanes).toHaveLength(1);
    expect(viewModel.lanes[0]?.mainOrchestratorThinking).toBe("");
    expect(viewModel.lanes[0]?.timelineItems).toContainEqual(
      expect.objectContaining({
        kind: "request",
        block: expect.objectContaining({
          id: "req-1",
          model: "gpt-test",
        }),
      }),
    );
    expect(viewModel.usageSummary.requestCount).toBe(1);
    expect(viewModel.usageSummary.cacheHitRate).toBeNull();
    expect(viewModel.usageBreakdowns).toEqual([
      expect.objectContaining({
        key: "gpt-test::openai",
        label: "gpt-test · openai",
        summary: expect.objectContaining({
          requestCount: 1,
        }),
      }),
    ]);
  });

  it("hides duplicate final-response events when the request block already carries the same reply", () => {
    const group = mapTraceToLiveGroup(
      {
        trace_id: "trace-final",
        session_key: "session-final",
        session_id: "session-final-id",
        chat_type: "group",
        chat_id: "chat-final",
        user_id: "user-final",
        message_id: "msg-final",
        started_at: 1,
        ended_at: 4,
        event_count: 4,
        status: "completed",
        latest_stage: "final",
        summary: { headline: "final trace" },
      },
      [
        {
          event_id: "evt-1",
          trace_id: "trace-final",
          span_id: "span-1",
          parent_span_id: "",
          ts: 1,
          session_key: "session-final",
          session_id: "session-final-id",
          platform: "napcat",
          chat_type: "group",
          chat_id: "chat-final",
          user_id: "user-final",
          message_id: "msg-final",
          event_type: "agent.model.requested",
          severity: "info",
          payload: {
            llm_request_id: "req-final",
            model: "gpt-test",
            provider: "openai",
            request_body: "{\"messages\":[\"hello\"]}",
            stage: "model",
          },
        },
        {
          event_id: "evt-2",
          trace_id: "trace-final",
          span_id: "span-2",
          parent_span_id: "span-1",
          ts: 2,
          session_key: "session-final",
          session_id: "session-final-id",
          platform: "napcat",
          chat_type: "group",
          chat_id: "chat-final",
          user_id: "user-final",
          message_id: "msg-final",
          event_type: "agent.response.final",
          severity: "info",
          payload: {
            llm_request_id: "req-final",
            response_preview: "final reply",
            stage: "final",
          },
        },
        {
          event_id: "evt-3",
          trace_id: "trace-final",
          span_id: "span-3",
          parent_span_id: "span-1",
          ts: 3,
          session_key: "session-final",
          session_id: "session-final-id",
          platform: "napcat",
          chat_type: "group",
          chat_id: "chat-final",
          user_id: "user-final",
          message_id: "msg-final",
          event_type: "agent.model.completed",
          severity: "info",
          payload: {
            llm_request_id: "req-final",
            model: "gpt-test",
            provider: "openai",
            total_tokens: 22,
            output_tokens: 11,
            duration_ms: 1000,
            stage: "model",
          },
        },
      ],
    );

    const viewModel = buildTraceDetailViewModel(group);
    const requestItem = viewModel.lanes[0]?.timelineItems.find((item) => item.kind === "request");

    expect(viewModel.lanes[0]?.timelineItems).toContainEqual(
      expect.objectContaining({
        kind: "request",
      }),
    );
    expect(viewModel.lanes[0]?.timelineItems).not.toContainEqual(
      expect.objectContaining({
        kind: "event",
        entry: expect.objectContaining({
          title: "agent.response.final",
        }),
      }),
    );
    expect(requestItem?.block?.responseText).toBe("final reply");
  });

  it("renders unscoped memory activity as an event instead of a request block", () => {
    const group = mapTraceToLiveGroup(
      {
        trace_id: "trace-2",
        session_key: "session-2",
        session_id: "session-2-id",
        chat_type: "group",
        chat_id: "chat-2",
        user_id: "user-2",
        message_id: "msg-2",
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
          trace_id: "trace-2",
          span_id: "span-memory",
          parent_span_id: "",
          ts: 1,
          session_key: "session-2",
          session_id: "session-2-id",
          platform: "napcat",
          chat_type: "group",
          chat_id: "chat-2",
          user_id: "user-2",
          message_id: "msg-2",
          event_type: "agent.memory.used",
          severity: "info",
          payload: {
            source: "auto_injection",
            summary: "Prefetched identity memory",
            stage: "memory_skill_routing",
          },
        },
      ],
    );

    const viewModel = buildTraceDetailViewModel(group);

    expect(viewModel.lanes).toHaveLength(1);
    expect(viewModel.lanes[0]?.requestCount).toBe(0);
    expect(viewModel.lanes[0]?.timelineItems).toContainEqual(
      expect.objectContaining({
        kind: "event",
        entry: expect.objectContaining({
          kind: "memory",
          summary: "Prefetched identity memory",
          llmRequestId: null,
        }),
      }),
    );
    expect(viewModel.lanes[0]?.timelineItems).not.toContainEqual(
      expect.objectContaining({
        kind: "request",
      }),
    );
  });

  it("extracts always-visible main orchestrator thinking from the latest live block", () => {
    const group = mapTraceToLiveGroup(
      {
        trace_id: "trace-orchestrator",
        session_key: "session-orchestrator",
        session_id: "session-orchestrator-id",
        chat_type: "group",
        chat_id: "chat-orchestrator",
        user_id: "user-orchestrator",
        message_id: "msg-orchestrator",
        started_at: 1,
        ended_at: null,
        event_count: 4,
        status: "in_progress",
        latest_stage: "model",
        summary: { headline: "orchestrator trace" },
      },
      [
        {
          event_id: "evt-1",
          trace_id: "trace-orchestrator",
          span_id: "span-1",
          parent_span_id: "",
          ts: 1,
          session_key: "session-orchestrator",
          session_id: "session-orchestrator-id",
          platform: "napcat",
          chat_type: "group",
          chat_id: "chat-orchestrator",
          user_id: "user-orchestrator",
          message_id: "msg-orchestrator",
          event_type: "orchestrator.turn.started",
          severity: "info",
          payload: { message_preview: "analyze", stage: "context" },
        },
        {
          event_id: "evt-2",
          trace_id: "trace-orchestrator",
          span_id: "span-2",
          parent_span_id: "span-1",
          ts: 2,
          session_key: "session-orchestrator",
          session_id: "session-orchestrator-id",
          platform: "napcat",
          chat_type: "group",
          chat_id: "chat-orchestrator",
          user_id: "user-orchestrator",
          message_id: "msg-orchestrator",
          event_type: "agent.model.requested",
          severity: "info",
          payload: {
            llm_request_id: "req-main",
            model: "router-model",
            provider: "openai",
            agent_scope: "main_orchestrator",
            request_body: "{\"messages\":[\"route\"]}",
            stage: "model",
          },
        },
        {
          event_id: "evt-3",
          trace_id: "trace-orchestrator",
          span_id: "span-3",
          parent_span_id: "span-2",
          ts: 2.1,
          session_key: "session-orchestrator",
          session_id: "session-orchestrator-id",
          platform: "napcat",
          chat_type: "group",
          chat_id: "chat-orchestrator",
          user_id: "user-orchestrator",
          message_id: "msg-orchestrator",
          event_type: "agent.reasoning.delta",
          severity: "info",
          payload: {
            llm_request_id: "req-main",
            agent_scope: "main_orchestrator",
            content: "thinking draft",
            sequence: 1,
            stage: "model",
          },
        },
        {
          event_id: "evt-4",
          trace_id: "trace-orchestrator",
          span_id: "span-4",
          parent_span_id: "span-2",
          ts: 2.2,
          session_key: "session-orchestrator",
          session_id: "session-orchestrator-id",
          platform: "napcat",
          chat_type: "group",
          chat_id: "chat-orchestrator",
          user_id: "user-orchestrator",
          message_id: "msg-orchestrator",
          event_type: "agent.reasoning.delta",
          severity: "info",
          payload: {
            llm_request_id: "req-main",
            agent_scope: "main_orchestrator",
            content: "thinking draft expanded",
            sequence: 2,
            stage: "model",
          },
        },
      ],
    );

    const viewModel = buildTraceDetailViewModel(group);

    expect(viewModel.lanes).toHaveLength(1);
    expect(viewModel.lanes[0]?.key).toBe("main_orchestrator");
    expect(viewModel.lanes[0]?.mainOrchestratorThinking).toBe("thinking draft expanded");
    const requestBlock = viewModel.lanes[0]?.timelineItems.find((item) => item.kind === "request")?.block;
    expect(requestBlock?.thinkingSteps).toEqual([
      expect.objectContaining({
        sequence: 2,
        text: "thinking draft expanded",
      }),
    ]);
  });

  it("maps summary timing breakdown into the overview timing model", () => {
    const group = mapTraceToLiveGroup(
      {
        trace_id: "trace-timing",
        session_key: "session-timing",
        session_id: "session-timing-id",
        chat_type: "private",
        chat_id: "chat-timing",
        user_id: "user-timing",
        message_id: "msg-timing",
        started_at: 10,
        ended_at: 17,
        event_count: 3,
        status: "completed",
        latest_stage: "final",
        summary: {
          headline: "timing trace",
          timing_totals: {
            end_to_end_ms: 7600,
            measured_ms: 7500,
            unaccounted_ms: 100,
          },
          timing_breakdown: [
            {
              key: "gateway.turn_prepare",
              label: "Gateway Turn Prepare",
              duration_ms: 1200,
              started_at: 10,
              ended_at: 11.2,
              source_event_type: "gateway.turn.prepared",
            },
            {
              key: "response.send",
              label: "Response Send",
              duration_ms: 400,
              started_at: 17.2,
              ended_at: 17.6,
              source_event_type: "adapter.ws_action",
              note: "send_private_msg",
            },
          ],
        },
      },
      [
        {
          event_id: "evt-1",
          trace_id: "trace-timing",
          span_id: "span-1",
          parent_span_id: "",
          ts: 10.05,
          session_key: "session-timing",
          session_id: "session-timing-id",
          platform: "napcat",
          chat_type: "private",
          chat_id: "chat-timing",
          user_id: "user-timing",
          message_id: "msg-timing",
          event_type: "message.received",
          severity: "info",
          payload: { text_preview: "ping", stage: "inbound" },
        },
        {
          event_id: "evt-2",
          trace_id: "trace-timing",
          span_id: "span-2",
          parent_span_id: "span-1",
          ts: 16.5,
          session_key: "session-timing",
          session_id: "session-timing-id",
          platform: "napcat",
          chat_type: "private",
          chat_id: "chat-timing",
          user_id: "user-timing",
          message_id: "msg-timing",
          event_type: "orchestrator.turn.completed",
          severity: "info",
          payload: { reply_text: "done", stage: "final" },
        },
        {
          event_id: "evt-3",
          trace_id: "trace-timing",
          span_id: "span-3",
          parent_span_id: "span-2",
          ts: 17.6,
          session_key: "session-timing",
          session_id: "session-timing-id",
          platform: "napcat",
          chat_type: "private",
          chat_id: "chat-timing",
          user_id: "user-timing",
          message_id: "msg-timing",
          event_type: "adapter.ws_action",
          severity: "info",
          payload: { action: "send_private_msg", success: true, duration_ms: 400, stage: "raw" },
        },
      ],
    );

    const viewModel = buildTraceDetailViewModel(group);

    expect(viewModel.timingSummary?.endToEndMs).toBe(7600);
    expect(viewModel.timingSummary?.measuredMs).toBe(7500);
    expect(viewModel.timingSummary?.unaccountedMs).toBe(100);
    expect(viewModel.timingSummary?.items).toHaveLength(2);
    expect(viewModel.timingSummary?.items[0]).toEqual(
      expect.objectContaining({
        key: "gateway.turn_prepare",
        label: "Gateway Turn Prepare",
        durationMs: 1200,
      }),
    );
    expect(viewModel.timingSummary?.items[0]?.shareOfTotal).toBeCloseTo(1200 / 7600, 6);
    expect(viewModel.timingSummary?.items[1]).toEqual(
      expect.objectContaining({
        key: "response.send",
        label: "Response Send",
        durationMs: 400,
        note: "send_private_msg",
      }),
    );
    expect(viewModel.timingSummary?.items[1]?.shareOfTotal).toBeCloseTo(400 / 7600, 6);
  });
});
