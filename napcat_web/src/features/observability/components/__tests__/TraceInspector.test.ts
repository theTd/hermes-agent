import { describe, expect, it } from "vitest";

import {
  buildLlmUsageCostBreakdowns,
  collectContextTextBlocks,
  collectLlmUsageCostItems,
  collectTriggerTextBlocks,
  summarizeLlmUsageCostItems,
} from "../TraceInspector";

describe("collectContextTextBlocks", () => {
  it("prefers the latest raw LLM request body over debug sub-blocks", () => {
    const blocks = collectContextTextBlocks([
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
        event_type: "agent.run.started",
        severity: "info",
        payload: {
          prompt: "完整 prompt",
          user_message: "hi",
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
        event_type: "agent.model.requested",
        severity: "info",
        payload: {
          model: "MiniMax-M2.7-highspeed",
          request_body: "{\n  \"model\": \"MiniMax-M2.7-highspeed\",\n  \"messages\": [\n    {\n      \"role\": \"user\",\n      \"content\": \"hi\"\n    }\n  ]\n}",
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
        event_type: "agent.memory.used",
        severity: "info",
        payload: {
          stage: "context",
          source: "auto_injection",
          memory_prefetch_fenced: "<memory-context>\nUser is Alice\n</memory-context>",
          memory_prefetch_params_by_provider: "## lightrag\n{\n  \"lane\": \"identity\",\n  \"query_mode\": \"mix\"\n}",
          user_message_before_auto_injection: "hi",
          user_message_after_auto_injection: "hi\n\n<memory-context>...</memory-context>",
        },
      },
    ]);

    expect(blocks).toEqual([
      {
        label: "2.agent.model.requested.request_body",
        content: "{\n  \"model\": \"MiniMax-M2.7-highspeed\",\n  \"messages\": [\n    {\n      \"role\": \"user\",\n      \"content\": \"hi\"\n    }\n  ]\n}",
      },
      {
        label: "3.agent.memory.used.memory_prefetch_fenced",
        content: "<memory-context>\nUser is Alice\n</memory-context>",
      },
      {
        label: "3.agent.memory.used.memory_prefetch_params_by_provider",
        content: "## lightrag\n{\n  \"lane\": \"identity\",\n  \"query_mode\": \"mix\"\n}",
      },
    ]);
  });

  it("falls back to prompt and auto-injection debug fields when raw request body is unavailable", () => {
    const blocks = collectContextTextBlocks([
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
        event_type: "agent.run.started",
        severity: "info",
        payload: {
          prompt: "完整 prompt",
          user_message: "hi",
        },
      },
      {
        event_id: "evt-2",
        trace_id: "trace-1",
        span_id: "span-2",
        parent_span_id: "span-1",
        ts: 2,
        session_key: "session-1",
        session_id: "sess-1",
        platform: "napcat",
        chat_type: "group",
        chat_id: "chat-1",
        user_id: "user-1",
        message_id: "msg-1",
        event_type: "agent.memory.used",
        severity: "info",
        payload: {
          stage: "context",
          source: "auto_injection",
          user_message_before_auto_injection: "hi",
          user_message_after_auto_injection: "hi\n\n<memory-context>...</memory-context>",
        },
      },
    ]);

    expect(blocks.map((block) => block.label)).toEqual([
      "1.agent.run.started.prompt",
      "1.agent.run.started.user_message",
      "2.agent.memory.used.user_message_before_auto_injection",
      "2.agent.memory.used.user_message_after_auto_injection",
    ]);
    expect(blocks[0]?.content).toBe("完整 prompt");
    expect(blocks[3]?.content).toContain("<memory-context>");
  });
});

describe("collectTriggerTextBlocks", () => {
  it("returns no trigger text blocks after llm gate removal", () => {
    const blocks = collectTriggerTextBlocks([
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
        event_type: "group.trigger.evaluated",
        severity: "info",
        payload: {
          trigger_reason: "group_message",
          raw_text_preview: "hi",
        },
      },
    ]);

    expect(blocks).toEqual([]);
  });
});

describe("collectLlmUsageCostItems", () => {
  it("extracts model usage and cost details", () => {
    const items = collectLlmUsageCostItems([
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
        event_type: "agent.model.completed",
        severity: "info",
        payload: {
          model: "MiniMax-M2.7-highspeed",
          provider: "minimax",
          api_calls: 2,
          input_tokens: 120,
          output_tokens: 34,
          cache_read_tokens: 5,
          cache_write_tokens: 6,
          reasoning_tokens: 7,
          prompt_tokens: 131,
          total_tokens: 165,
          estimated_cost_usd: 0.0123,
          cost_status: "estimated",
          cost_source: "pricing_table",
          duration_ms: 456,
        },
      },
    ], "model");

    expect(items).toEqual([
      {
        id: "evt-1:0",
        label: "LLM Call #1",
        model: "MiniMax-M2.7-highspeed",
        provider: "minimax",
        apiCalls: 2,
        promptTokens: 131,
        inputTokens: 120,
        outputTokens: 34,
        cacheReadTokens: 5,
        cacheWriteTokens: 6,
        reasoningTokens: 7,
        totalTokens: 165,
        durationMs: 456,
        costUsd: 0.0123,
        costLabel: "~$0.0123",
        status: "estimated",
        source: "pricing_table",
      },
    ]);
  });

  it("ignores trigger-stage usage entries after llm gate removal", () => {
    const items = collectLlmUsageCostItems([
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
        event_type: "group.trigger.evaluated",
        severity: "info",
        payload: {
          trigger_reason: "group_message",
          model: "qwen-flash",
          provider: "alibaba",
          api_calls: 1,
          input_tokens: 12,
          output_tokens: 8,
          cache_read_tokens: 3,
          prompt_tokens: 15,
          total_tokens: 23,
          estimated_cost_usd: 0.0004,
          cost_status: "estimated",
          cost_source: "pricing_table",
          duration_ms: 123,
        },
      },
    ], "trigger");

    expect(items).toEqual([]);
  });
});

describe("summarizeLlmUsageCostItems", () => {
  it("aggregates total cost and token usage across all llm requests", () => {
    const summary = summarizeLlmUsageCostItems([
      {
        id: "evt-1:0",
        label: "LLM Gate",
        model: "qwen-flash",
        provider: "alibaba",
        apiCalls: 1,
        promptTokens: 15,
        inputTokens: 12,
        outputTokens: 8,
        cacheReadTokens: 3,
        cacheWriteTokens: 0,
        reasoningTokens: 0,
        totalTokens: 23,
        durationMs: 123,
        costUsd: 0.0004,
        costLabel: "~$0.0004",
        status: "estimated",
        source: "pricing_table",
      },
      {
        id: "evt-2:1",
        label: "LLM Call #2",
        model: "MiniMax-M2.7-highspeed",
        provider: "minimax",
        apiCalls: 2,
        promptTokens: 131,
        inputTokens: 120,
        outputTokens: 34,
        cacheReadTokens: 5,
        cacheWriteTokens: 6,
        reasoningTokens: 7,
        totalTokens: 165,
        durationMs: 456,
        costUsd: 0.0123,
        costLabel: "~$0.0123",
        status: "estimated",
        source: "pricing_table",
      },
      {
        id: "evt-3:2",
        label: "LLM Call #3",
        model: "unknown",
        provider: "custom",
        apiCalls: 1,
        promptTokens: 9,
        inputTokens: 9,
        outputTokens: 3,
        cacheReadTokens: 0,
        cacheWriteTokens: 0,
        reasoningTokens: 0,
        totalTokens: 12,
        durationMs: 50,
        costUsd: null,
        costLabel: "unknown",
        status: "unknown",
        source: "none",
      },
    ]);

    expect(summary).toEqual({
      key: "all-models",
      label: "All Models",
      model: "",
      provider: "",
      requestCount: 3,
      apiCalls: 4,
      promptTokens: 155,
      inputTokens: 141,
      outputTokens: 45,
      cacheReadTokens: 8,
      cacheWriteTokens: 6,
      reasoningTokens: 7,
      totalTokens: 200,
      durationMs: 629,
      costUsd: 0.0127,
      costLabel: "~$0.0127 + 1 unresolved",
      estimatedCount: 2,
      includedCount: 0,
      unknownCount: 1,
      cacheHitRate: 8 / 155,
    });
  });
});

describe("buildLlmUsageCostBreakdowns", () => {
  it("groups usage stats by model and provider", () => {
    const breakdowns = buildLlmUsageCostBreakdowns([
      {
        id: "evt-1:0",
        label: "LLM Call #1",
        model: "gpt-4.1",
        provider: "openai",
        apiCalls: 1,
        promptTokens: 100,
        inputTokens: 80,
        outputTokens: 20,
        cacheReadTokens: 50,
        cacheWriteTokens: 0,
        reasoningTokens: 0,
        totalTokens: 120,
        durationMs: 200,
        costUsd: 0.01,
        costLabel: "~$0.0100",
        status: "estimated",
        source: "pricing_table",
      },
      {
        id: "evt-2:1",
        label: "LLM Call #2",
        model: "gpt-4.1",
        provider: "openai",
        apiCalls: 1,
        promptTokens: 40,
        inputTokens: 30,
        outputTokens: 10,
        cacheReadTokens: 10,
        cacheWriteTokens: 0,
        reasoningTokens: 0,
        totalTokens: 50,
        durationMs: 100,
        costUsd: 0.005,
        costLabel: "~$0.0050",
        status: "estimated",
        source: "pricing_table",
      },
      {
        id: "evt-3:2",
        label: "LLM Call #3",
        model: "claude-sonnet",
        provider: "anthropic",
        apiCalls: 1,
        promptTokens: 20,
        inputTokens: 20,
        outputTokens: 5,
        cacheReadTokens: 0,
        cacheWriteTokens: 0,
        reasoningTokens: 3,
        totalTokens: 25,
        durationMs: 80,
        costUsd: 0.003,
        costLabel: "~$0.0030",
        status: "estimated",
        source: "pricing_table",
      },
    ]);

    expect(breakdowns).toHaveLength(2);
    expect(breakdowns[0]).toEqual(
      expect.objectContaining({
        key: "gpt-4.1::openai",
        label: "gpt-4.1 · openai",
        summary: expect.objectContaining({
          requestCount: 2,
          promptTokens: 140,
          cacheReadTokens: 60,
          cacheHitRate: 60 / 140,
        }),
      }),
    );
    expect(breakdowns[1]).toEqual(
      expect.objectContaining({
        key: "claude-sonnet::anthropic",
        label: "claude-sonnet · anthropic",
        summary: expect.objectContaining({
          requestCount: 1,
          promptTokens: 20,
          cacheReadTokens: 0,
          cacheHitRate: 0,
        }),
      }),
    );
  });
});
