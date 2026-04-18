import type { NapcatEvent } from "@/lib/napcat-api";
import type {
  LiveTraceGroup,
  LlmRequestBlock,
  TimelineEntry,
} from "../types";

function fmtMoney(value: unknown, status: unknown): string {
  if (status === "included") {
    return "included";
  }
  const amount = Number(value);
  if (!Number.isFinite(amount)) {
    return "unknown";
  }
  const prefix = status === "estimated" ? "~" : "";
  return `${prefix}$${amount.toFixed(4)}`;
}

function toInt(value: unknown): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? Math.max(0, Math.trunc(parsed)) : 0;
}

function extractNamedTextBlocks(
  payload: Record<string, unknown> | null | undefined,
  fields: string[],
  options?: { labelPrefix?: string },
): Array<{ label: string; content: string }> {
  if (!payload) {
    return [];
  }
  const labelPrefix = options?.labelPrefix ?? "";
  return fields.flatMap((field) => {
    const value = payload[field];
    if (typeof value !== "string" || !value.trim()) {
      return [];
    }
    return [{ label: labelPrefix ? `${labelPrefix}.${field}` : field, content: value }];
  });
}

const CONTEXT_TEXT_FIELDS = [
  "prompt",
  "context",
  "platform_prompt",
  "default_prompt",
  "memory_prefetch_by_provider",
  "memory_prefetch_content",
  "memory_prefetch_fenced",
  "memory_prefetch_params_by_provider",
  "plugin_user_context",
  "user_message_before_auto_injection",
  "user_message_after_auto_injection",
  "user_message",
];

function latestRawRequestBodyBlock(
  events: NapcatEvent[],
  options?: { agentScope?: string },
): Array<{ label: string; content: string }> {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index];
    if (event?.event_type !== "agent.model.requested") {
      continue;
    }
    if (options?.agentScope && String(event.payload?.agent_scope || "") !== options.agentScope) {
      continue;
    }
    const requestBody = event.payload?.request_body;
    if (typeof requestBody !== "string" || !requestBody.trim()) {
      continue;
    }
    return [{
      label: `${index + 1}.${event.event_type}.request_body`,
      content: requestBody,
    }];
  }
  return [];
}

function latestInjectedMemoryBlocks(
  events: NapcatEvent[],
): Array<{ label: string; content: string }> {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index];
    if (event?.event_type !== "agent.memory.used") {
      continue;
    }
    if (event.payload?.source !== "auto_injection") {
      continue;
    }
    const payload = (event.payload ?? null) as Record<string, unknown> | null;
    if (!payload) {
      continue;
    }
    const prefix = `${index + 1}.${event.event_type}`;
    return extractNamedTextBlocks(
      payload,
      ["memory_prefetch_fenced", "memory_prefetch_params_by_provider"],
      { labelPrefix: prefix },
    );
  }
  return [];
}

export interface LlmUsageCostItem {
  id: string;
  label: string;
  model: string;
  provider: string;
  apiCalls: number;
  promptTokens: number;
  inputTokens: number;
  outputTokens: number;
  cacheReadTokens: number;
  cacheWriteTokens: number;
  reasoningTokens: number;
  totalTokens: number;
  durationMs: number | null;
  costUsd: number | null;
  costLabel: string;
  status: string;
  source: string;
}

export interface LlmUsageCostSummary {
  requestCount: number;
  apiCalls: number;
  promptTokens: number;
  inputTokens: number;
  outputTokens: number;
  cacheReadTokens: number;
  cacheWriteTokens: number;
  reasoningTokens: number;
  totalTokens: number;
  durationMs: number;
  costUsd: number | null;
  costLabel: string;
  estimatedCount: number;
  includedCount: number;
  unknownCount: number;
}

export interface TraceTimelineItemViewModel {
  id: string;
  kind: "request" | "event";
  block?: LlmRequestBlock;
  entry?: TimelineEntry;
}

export interface TraceDetailLaneViewModel {
  key: string;
  title: string;
  status: string;
  model: string;
  provider: string;
  requestCount: number;
  mainOrchestratorThinking: string;
  timelineItems: TraceTimelineItemViewModel[];
}

export interface TraceDetailViewModel {
  group: LiveTraceGroup;
  primaryOverviewEntries: Array<{ label: string; value: string }>;
  secondaryOverviewEntries: Array<{ label: string; value: string }>;
  usageSummary: LlmUsageCostSummary;
  rawContextBlocks: Array<{ label: string; content: string }>;
  lanes: TraceDetailLaneViewModel[];
}

export function collectLlmUsageCostItems(
  events: NapcatEvent[] | undefined,
  scope: "model" | "trigger",
): LlmUsageCostItem[] {
  if (!events?.length) {
    return [];
  }
  return events.flatMap((event, index) => {
    const payload = event.payload ?? {};
    const isModelCall = scope === "model" && event.event_type === "agent.model.completed";
    if (!isModelCall) {
      return [];
    }

    const promptTokens = toInt(payload.prompt_tokens);
    const inputTokens = toInt(payload.input_tokens);
    const outputTokens = toInt(payload.output_tokens);
    const cacheReadTokens = toInt(payload.cache_read_tokens);
    const cacheWriteTokens = toInt(payload.cache_write_tokens);
    const reasoningTokens = toInt(payload.reasoning_tokens);
    const totalTokens = toInt(payload.total_tokens) || promptTokens + outputTokens;
    if (totalTokens <= 0 && payload.estimated_cost_usd == null && payload.cost_status == null) {
      return [];
    }

    return [{
      id: `${event.event_id}:${index}`,
      label: `LLM Call #${index + 1}`,
      model: String(payload.model || ""),
      provider: String(payload.provider || ""),
      apiCalls: toInt(payload.api_calls) || 1,
      promptTokens,
      inputTokens,
      outputTokens,
      cacheReadTokens,
      cacheWriteTokens,
      reasoningTokens,
      totalTokens,
      durationMs: Number.isFinite(Number(payload.duration_ms)) ? Number(payload.duration_ms) : null,
      costUsd: Number.isFinite(Number(payload.estimated_cost_usd)) ? Number(payload.estimated_cost_usd) : null,
      costLabel: fmtMoney(payload.estimated_cost_usd, payload.cost_status),
      status: String(payload.cost_status || "unknown"),
      source: String(payload.cost_source || ""),
    }];
  });
}

function formatTotalCostLabel(
  costUsd: number | null,
  estimatedCount: number,
  includedCount: number,
  unknownCount: number,
): string {
  if (costUsd != null) {
    const prefix = estimatedCount > 0 ? "~" : "";
    const suffixCount = includedCount + unknownCount;
    return suffixCount > 0
      ? `${prefix}$${costUsd.toFixed(4)} + ${suffixCount} unresolved`
      : `${prefix}$${costUsd.toFixed(4)}`;
  }
  if (includedCount > 0 && unknownCount === 0) {
    return "included";
  }
  if (unknownCount > 0) {
    return `unknown (${unknownCount})`;
  }
  return "unknown";
}

export function summarizeLlmUsageCostItems(items: LlmUsageCostItem[]): LlmUsageCostSummary {
  const estimatedCount = items.filter((item) => item.status === "estimated").length;
  const includedCount = items.filter((item) => item.status === "included" && item.costUsd == null).length;
  const unknownCount = items.filter((item) => item.costUsd == null && item.status !== "included").length;
  const knownCostUsd = Number(items.reduce((sum, item) => sum + (item.costUsd ?? 0), 0).toFixed(6));
  const hasKnownCost = items.some((item) => item.costUsd != null);
  const durationMs = items.reduce((sum, item) => sum + (item.durationMs ?? 0), 0);
  return {
    requestCount: items.length,
    apiCalls: items.reduce((sum, item) => sum + item.apiCalls, 0),
    promptTokens: items.reduce((sum, item) => sum + item.promptTokens, 0),
    inputTokens: items.reduce((sum, item) => sum + item.inputTokens, 0),
    outputTokens: items.reduce((sum, item) => sum + item.outputTokens, 0),
    cacheReadTokens: items.reduce((sum, item) => sum + item.cacheReadTokens, 0),
    cacheWriteTokens: items.reduce((sum, item) => sum + item.cacheWriteTokens, 0),
    reasoningTokens: items.reduce((sum, item) => sum + item.reasoningTokens, 0),
    totalTokens: items.reduce((sum, item) => sum + item.totalTokens, 0),
    durationMs,
    costUsd: hasKnownCost ? knownCostUsd : null,
    costLabel: formatTotalCostLabel(hasKnownCost ? knownCostUsd : null, estimatedCount, includedCount, unknownCount),
    estimatedCount,
    includedCount,
    unknownCount,
  };
}

export function collectContextTextBlocks(
  events: NapcatEvent[] | undefined,
): Array<{ label: string; content: string }> {
  if (!events?.length) {
    return [];
  }

  const prioritizedBlocks = [
    ...latestRawRequestBodyBlock(events),
    ...latestInjectedMemoryBlocks(events),
  ];
  if (prioritizedBlocks.length > 0) {
    return prioritizedBlocks;
  }

  return events.flatMap((event, index) => {
    const payload = (event?.payload ?? null) as Record<string, unknown> | null;
    const prefix = `${index + 1}.${event.event_type}`;
    return extractNamedTextBlocks(payload, CONTEXT_TEXT_FIELDS, { labelPrefix: prefix });
  });
}

export function collectTriggerTextBlocks(
  events: NapcatEvent[] | undefined,
): Array<{ label: string; content: string }> {
  void events;
  return [];
}

function pickMainOrchestratorThinking(group: LiveTraceGroup, laneKey: string): string {
  if (laneKey !== "main_orchestrator") {
    return "";
  }

  const lane = group.timeline.lanes.find((item) => item.key === laneKey);
  if (!lane) {
    return "";
  }

  const runningBlock = [...lane.requestBlocks]
    .reverse()
    .find((block) => block.status === "running" && block.thinkingText.trim());
  if (runningBlock) {
    return runningBlock.thinkingText;
  }

  const latestCompletedBlock = [...lane.requestBlocks]
    .reverse()
    .find((block) => block.thinkingText.trim());
  return latestCompletedBlock?.thinkingText ?? "";
}

export function buildTraceDetailViewModel(group: LiveTraceGroup): TraceDetailViewModel {
  const usageSummary = summarizeLlmUsageCostItems(collectLlmUsageCostItems(group.rawEvents, "model"));
  const overviewEntries = [
    { label: "trace_id", value: group.traceId },
    { label: "status", value: group.status },
    { label: "session", value: group.trace.session_key },
    { label: "chat", value: `${group.trace.chat_type}:${group.trace.chat_id}` },
    { label: "user", value: group.trace.user_id },
    { label: "message", value: group.trace.message_id },
    { label: "lanes", value: String(group.timeline.laneCount) },
    { label: "llm_requests", value: String(group.timeline.requestCount) },
  ];

  return {
    group,
    primaryOverviewEntries: overviewEntries.filter((entry) =>
      ["trace_id", "session", "chat", "llm_requests"].includes(entry.label),
    ),
    secondaryOverviewEntries: overviewEntries.filter((entry) =>
      ["user", "message", "lanes", "status"].includes(entry.label),
    ),
    usageSummary,
    rawContextBlocks: collectContextTextBlocks(group.rawEvents),
    lanes: group.timeline.lanes.map((lane) => ({
      key: lane.key,
      title: lane.title,
      status: lane.status,
      model: lane.model,
      provider: lane.provider,
      requestCount: lane.requestBlocks.length,
      mainOrchestratorThinking: pickMainOrchestratorThinking(group, lane.key),
      timelineItems: lane.timelineEntries.map((entry) => {
        if (entry.kind === "llm_request" && entry.llmRequestId) {
          const block = lane.requestBlocks.find((item) => item.id === entry.llmRequestId);
          if (block) {
            return {
              id: entry.id,
              kind: "request" as const,
              block,
            };
          }
        }
        return {
          id: entry.id,
          kind: "event" as const,
          entry,
        };
      }),
    })),
  };
}
