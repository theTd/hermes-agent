import type { NapcatEvent, NapcatTrace } from "@/lib/napcat-api";
import type {
  AgentLane,
  ChildTraceDetail,
  ChildTaskItem,
  ChildTaskStatus,
  LlmRequestBlock,
  LlmUsageSnapshot,
  LiveTraceGroup,
  MainOrchestratorState,
  StreamChunk,
  ThinkingStep,
  TimelineEntry,
  ToolCallItem,
  TraceTimeline,
  TraceStage,
  TraceStageKey,
  TraceStageStatus,
} from "./types";

const STAGE_ORDER: TraceStageKey[] = [
  "inbound",
  "trigger",
  "context",
  "model",
  "tools",
  "memory_skill_routing",
  "final",
  "error",
  "raw",
];

const STAGE_TITLES: Record<TraceStageKey, string> = {
  inbound: "Inbound",
  trigger: "Trigger",
  context: "Context / Prompt",
  model: "Model / Streaming",
  tools: "Tools",
  memory_skill_routing: "Memory / Skill / Routing",
  final: "Final",
  error: "Error",
  raw: "Raw",
};

const STAGE_PRIORITY: Record<TraceStageKey, number> = {
  raw: 0,
  inbound: 1,
  trigger: 2,
  context: 3,
  model: 4,
  tools: 5,
  memory_skill_routing: 6,
  final: 7,
  error: 8,
};

export function sortTraceEvents(events: NapcatEvent[]): NapcatEvent[] {
  return [...events].sort((left, right) => {
    if (left.ts !== right.ts) {
      return left.ts - right.ts;
    }
    const leftSequence = Number(left.payload?.sequence ?? 0);
    const rightSequence = Number(right.payload?.sequence ?? 0);
    if (leftSequence !== rightSequence) {
      return leftSequence - rightSequence;
    }
    return left.event_id.localeCompare(right.event_id);
  });
}

export function fallbackStageForEvent(event: NapcatEvent): TraceStageKey {
  const stage = String(event.payload?.stage || "").trim();
  if (STAGE_ORDER.includes(stage as TraceStageKey)) {
    return stage as TraceStageKey;
  }

  const eventType = event.event_type;
  if (eventType.startsWith("message.")) return "inbound";
  if (eventType.startsWith("group.trigger.") || eventType === "group.context_only_emitted") return "trigger";
  if (eventType === "gateway.turn.prepared" || eventType === "agent.run.started") return "context";
  if (eventType === "orchestrator.turn.started" || eventType === "orchestrator.decision.parsed") return "context";
  if (eventType === "orchestrator.turn.completed" || eventType === "orchestrator.turn.ignored") return "final";
  if (eventType === "orchestrator.turn.failed") return "error";
  if (eventType.startsWith("agent.model.") || eventType === "agent.reasoning.delta" || eventType === "agent.response.delta") return "model";
  if (eventType === "orchestrator.child.spawned" || eventType === "orchestrator.child.reused") return "tools";
  if (eventType === "orchestrator.child.completed" || eventType === "orchestrator.child.cancelled") return "final";
  if (eventType === "orchestrator.child.failed") return "error";
  if (eventType.startsWith("agent.tool.")) return "tools";
  if (
    eventType === "agent.memory.called"
    || eventType === "agent.memory.used"
    || eventType === "agent.skill.called"
    || eventType === "agent.skill.used"
    || eventType === "policy.applied"
  ) return "memory_skill_routing";
  if (
    eventType === "agent.response.final"
    || eventType === "agent.response.suppressed"
    || eventType === "gateway.turn.short_circuited"
  ) return "final";
  if (eventType === "error.raised") return "error";
  return "raw";
}

function explicitToolCallIdForEvent(event: NapcatEvent): string {
  const explicitId = String(event.payload?.tool_call_id || "").trim();
  if (explicitId) {
    return explicitId;
  }
  return "";
}

function buildToolCalls(events: NapcatEvent[]): ToolCallItem[] {
  const map = new Map<string, ToolCallItem>();
  const openFallbackIdsByTool = new Map<string, string[]>();

  for (const event of events) {
    if (!event.event_type.startsWith("agent.tool.")) {
      continue;
    }

    const toolName = String(event.payload?.tool_name || "tool").trim() || "tool";
    const explicitId = explicitToolCallIdForEvent(event);
    let id = explicitId;
    if (!id) {
      const queue = openFallbackIdsByTool.get(toolName) ?? [];
      if (event.event_type === "agent.tool.called") {
        id = `${toolName}:${event.ts}:${event.trace_id}`;
        queue.push(id);
        openFallbackIdsByTool.set(toolName, queue);
      } else {
        id = queue.shift() ?? `${toolName}:${event.ts}:${event.trace_id}`;
        openFallbackIdsByTool.set(toolName, queue);
      }
    }
    const existing = map.get(id) ?? {
      id,
      toolName,
      status: "running",
      startedAt: event.ts,
      endedAt: null,
      durationMs: null,
      argsPreview: "",
      resultPreview: "",
      stdoutPreview: "",
      stderrPreview: "",
      error: "",
    };

    const next: ToolCallItem = {
      ...existing,
      toolName: existing.toolName || toolName,
      startedAt: Math.min(existing.startedAt, event.ts),
      argsPreview: existing.argsPreview || String(event.payload?.args_preview || ""),
      resultPreview: existing.resultPreview || String(event.payload?.result_preview || ""),
      stdoutPreview: existing.stdoutPreview || String(event.payload?.stdout_preview || ""),
      stderrPreview: existing.stderrPreview || String(event.payload?.stderr_preview || ""),
      error: existing.error || String(event.payload?.error || ""),
    };

    if (event.event_type === "agent.tool.completed") {
      next.status = "completed";
      next.endedAt = event.ts;
    } else if (event.event_type === "agent.tool.failed") {
      next.status = "failed";
      next.endedAt = event.ts;
      next.error = next.error || String(event.payload?.error || "Tool failed");
    } else {
      next.status = next.status === "failed" ? "failed" : "running";
    }

    const duration = resolveDurationMs(event.payload);
    if (duration !== null) {
      next.durationMs = duration;
    } else if (next.endedAt != null) {
      next.durationMs = (next.endedAt - next.startedAt) * 1000;
    }

    map.set(id, next);
  }

  return [...map.values()].sort((left, right) => {
    if (left.startedAt !== right.startedAt) {
      return left.startedAt - right.startedAt;
    }
    return left.id.localeCompare(right.id);
  });
}

function buildStreamChunks(events: NapcatEvent[]): StreamChunk[] {
  const chunks: StreamChunk[] = [];
  const accumulatedByStream = new Map<string, { content: string; lastSequence: number }>();
  for (const event of events) {
    if (event.event_type !== "agent.reasoning.delta" && event.event_type !== "agent.response.delta") {
      continue;
    }
    const kind = event.event_type === "agent.reasoning.delta" ? "thinking" : "response";
    const agentScope = String(event.payload?.agent_scope || "");
    const childId = String(event.payload?.child_id || "");
    const llmRequestId = String(event.payload?.llm_request_id || "");
    const sequence = Number(event.payload?.sequence ?? 0);
    const baseKey = `${kind}:${agentScope}:${childId}:${llmRequestId || "legacy"}`;
    const previous = accumulatedByStream.get(baseKey) ?? { content: "", lastSequence: 0 };
    const explicitContent = typeof event.payload?.content === "string" ? event.payload.content : "";
    const delta = typeof event.payload?.delta === "string" ? event.payload.delta : "";
    const shouldResetLegacy = !llmRequestId && sequence > 0 && previous.lastSequence > 0 && sequence <= previous.lastSequence;
    // Guard against truncated content fields wiping earlier accumulated text.
    const useContent = explicitContent && explicitContent.length >= previous.content.length;
    const nextContent = useContent
      ? explicitContent
      : shouldResetLegacy
        ? delta
        : `${previous.content}${delta}`;
    accumulatedByStream.set(baseKey, {
      content: nextContent,
      lastSequence: sequence > 0 ? sequence : previous.lastSequence,
    });
    chunks.push({
      kind,
      content: nextContent,
      ts: event.ts,
      sequence,
      stage: fallbackStageForEvent(event),
      source: String(event.payload?.source || ""),
      agentScope,
      childId,
    });
  }
  return chunks.sort((left, right) => {
    if (left.ts !== right.ts) {
      return left.ts - right.ts;
    }
    if (left.sequence !== right.sequence) {
      return left.sequence - right.sequence;
    }
    return left.kind.localeCompare(right.kind);
  });
}

function deriveActiveStream(events: NapcatEvent[]): boolean | null {
  let activeStream: boolean | null = null;
  for (const event of events) {
    if (event.event_type === "agent.reasoning.delta" || event.event_type === "agent.response.delta") {
      activeStream = true;
      continue;
    }
    if (
      event.event_type === "agent.response.final"
      || event.event_type === "agent.response.suppressed"
      || event.event_type === "gateway.turn.short_circuited"
      || event.event_type === "error.raised"
      || event.event_type === "orchestrator.turn.completed"
      || event.event_type === "orchestrator.turn.failed"
      || event.event_type === "orchestrator.turn.ignored"
      || event.event_type === "orchestrator.child.completed"
      || event.event_type === "orchestrator.child.cancelled"
      || event.event_type === "orchestrator.child.failed"
    ) {
      activeStream = false;
    }
  }
  return activeStream;
}

function latestOf(events: NapcatEvent[], eventType: string): NapcatEvent | undefined {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    if (events[index]?.event_type === eventType) {
      return events[index];
    }
  }
  return undefined;
}

function trimText(value: unknown, maxChars: number = 240): string {
  const text = String(value ?? "").replace(/\s+/g, " ").trim();
  if (text.length <= maxChars) {
    return text;
  }
  if (looksLikeStructuredPayloadText(text)) {
    return text;
  }
  return `${text.slice(0, maxChars)}…`;
}

function parseStructuredPayloadCandidate(value: string): string | null {
  const trimmed = value.trim();
  if (!trimmed) {
    return null;
  }
  if (trimmed.startsWith("{") || trimmed.startsWith("[")) {
    return trimmed;
  }

  const objectIndex = trimmed.indexOf("{");
  const arrayIndex = trimmed.indexOf("[");
  const starts = [objectIndex, arrayIndex].filter((index) => index >= 0);
  if (starts.length === 0) {
    return null;
  }

  const startIndex = Math.min(...starts);
  const prefix = trimmed.slice(0, startIndex).trim();
  if (!prefix) {
    return trimmed.slice(startIndex);
  }

  const lines = prefix.split("\n").map((line) => line.trim()).filter(Boolean);
  if (lines.length === 0) {
    return trimmed.slice(startIndex);
  }

  const prefixLooksLikeDebugLabel = lines.every((line) => (
    /^#+\s*[\w./:-]+$/i.test(line)
    || /^[\w./:-]+$/i.test(line)
  ));
  return prefixLooksLikeDebugLabel ? trimmed.slice(startIndex) : null;
}

function looksLikeStructuredPayloadText(value: string): boolean {
  const candidate = parseStructuredPayloadCandidate(value);
  if (!candidate) {
    return false;
  }

  try {
    const parsed = JSON.parse(candidate);
    return Boolean(parsed) && typeof parsed === "object";
  } catch {
    return false;
  }
}

function toChildStatus(value: unknown, fallback: ChildTaskStatus): ChildTaskStatus {
  const status = String(value ?? "").trim();
  if (["queued", "running", "reused", "completed", "failed", "cancelled"].includes(status)) {
    return status as ChildTaskStatus;
  }
  return fallback;
}

function isChildTerminal(status: ChildTaskStatus): boolean {
  return ["completed", "failed", "cancelled"].includes(status);
}

function buildChildTasks(events: NapcatEvent[]): ChildTaskItem[] {
  const items = new Map<string, ChildTaskItem>();

  for (const event of events) {
    if (!event.event_type.startsWith("orchestrator.child.")) {
      continue;
    }
    const childId = String(event.payload?.child_id || "").trim();
    if (!childId) {
      continue;
    }

    const existing = items.get(childId) ?? {
      childId,
      goal: trimText(event.payload?.goal || "Background task"),
      requestKind: String(event.payload?.request_kind || "").trim(),
      runtimeSessionId: String(event.payload?.runtime_session_id || "").trim(),
      runtimeSessionKey: String(event.payload?.runtime_session_key || "").trim(),
      status: "queued" as ChildTaskStatus,
      startedAt: null,
      endedAt: null,
      durationMs: null,
      resultPreview: "",
      error: "",
      reason: "",
      summary: "",
      lastEventAt: event.ts,
      eventCount: 0,
      isActive: true,
      wasReused: false,
    };

    const next: ChildTaskItem = {
      ...existing,
      goal: trimText(event.payload?.goal || existing.goal || "Background task"),
      requestKind: String(event.payload?.request_kind || existing.requestKind || "").trim(),
      runtimeSessionId: String(event.payload?.runtime_session_id || existing.runtimeSessionId || "").trim(),
      runtimeSessionKey: String(event.payload?.runtime_session_key || existing.runtimeSessionKey || "").trim(),
      lastEventAt: Math.max(existing.lastEventAt, event.ts),
      eventCount: existing.eventCount + 1,
    };

    switch (event.event_type) {
      case "orchestrator.child.spawned":
        next.status = toChildStatus(event.payload?.status, existing.status === "reused" ? "reused" : "running");
        next.startedAt = existing.startedAt ?? event.ts;
        next.summary = trimText(event.payload?.summary || next.summary || "Started background task.");
        break;
      case "orchestrator.child.reused":
        next.status = "reused";
        next.wasReused = true;
        next.startedAt = existing.startedAt ?? event.ts;
        next.summary = trimText(event.payload?.summary || "Reused an existing active child task.");
        break;
      case "orchestrator.child.completed":
        next.status = "completed";
        next.startedAt = existing.startedAt ?? event.ts;
        next.endedAt = event.ts;
        next.resultPreview = trimText(
          event.payload?.result_preview || event.payload?.summary || existing.resultPreview,
        );
        next.summary = next.resultPreview || trimText(event.payload?.summary || "Child task completed.");
        break;
      case "orchestrator.child.failed":
        next.status = "failed";
        next.startedAt = existing.startedAt ?? event.ts;
        next.endedAt = event.ts;
        next.error = trimText(
          event.payload?.error || event.payload?.result_preview || existing.error || "Background task failed.",
        );
        next.resultPreview = trimText(event.payload?.result_preview || existing.resultPreview);
        next.summary = next.error || next.resultPreview || "Background task failed.";
        break;
      case "orchestrator.child.cancelled":
        next.status = "cancelled";
        next.startedAt = existing.startedAt ?? event.ts;
        next.endedAt = event.ts;
        next.reason = trimText(event.payload?.reason || existing.reason || "cancelled");
        next.summary = next.reason || "Child task was cancelled.";
        break;
      default:
        break;
    }

    next.isActive = !isChildTerminal(next.status);
    if (next.startedAt != null && next.endedAt != null) {
      next.durationMs = Math.max(0, (next.endedAt - next.startedAt) * 1000);
    }
    items.set(childId, next);
  }

  return [...items.values()].sort((left, right) => {
    if (left.isActive !== right.isActive) {
      return left.isActive ? -1 : 1;
    }
    if (left.lastEventAt !== right.lastEventAt) {
      return right.lastEventAt - left.lastEventAt;
    }
    return left.childId.localeCompare(right.childId);
  });
}

function childStatusToTraceStatus(child: ChildTaskItem): string {
  if (child.status === "failed") {
    return "error";
  }
  if (child.status === "queued" || child.status === "running" || child.status === "reused") {
    return "in_progress";
  }
  return "completed";
}

function eventBelongsToChild(event: NapcatEvent, childId: string): boolean {
  return String(event.payload?.child_id || "").trim() === childId;
}

function buildChildDetails(
  events: NapcatEvent[],
  childTasks: ChildTaskItem[],
): Record<string, ChildTraceDetail> {
  const detailsById: Record<string, ChildTraceDetail> = {};

  for (const child of childTasks) {
    const childEvents = sortTraceEvents(events.filter((event) => eventBelongsToChild(event, child.childId)));
    const groupedEvents = new Map<TraceStageKey, NapcatEvent[]>();
    for (const stage of STAGE_ORDER) {
      groupedEvents.set(stage, []);
    }
    for (const event of childEvents) {
      const stage = fallbackStageForEvent(event);
      groupedEvents.get(stage)?.push(event);
    }

    let latestStage: TraceStageKey = "raw";
    for (const event of childEvents) {
      const stage = fallbackStageForEvent(event);
      if (STAGE_PRIORITY[stage] >= STAGE_PRIORITY[latestStage]) {
        latestStage = stage;
      }
    }

    const streamChunks = buildStreamChunks(childEvents);
    const toolCalls = buildToolCalls(childEvents);
    const status = childStatusToTraceStatus(child);
    const activeStream = child.isActive && streamChunks.length > 0;
    const lastEventAt = childEvents[childEvents.length - 1]?.ts ?? child.lastEventAt;

    detailsById[child.childId] = {
      childId: child.childId,
      task: child,
      status,
      startedAt: child.startedAt,
      endedAt: child.endedAt,
      durationMs: child.durationMs,
      latestStage,
      activeStream,
      eventCount: childEvents.length,
      toolCallCount: toolCalls.length,
      errorCount: childEvents.filter((event) =>
        event.event_type === "error.raised" || event.event_type === "orchestrator.child.failed",
      ).length,
      model: String(childEvents.find((event) => event.payload?.model)?.payload?.model || ""),
      lastEventAt,
      stages: buildStages(groupedEvents, latestStage, status, activeStream, toolCalls, streamChunks),
      toolCalls,
      streamChunks,
      rawEvents: childEvents,
    };
  }

  return detailsById;
}

function buildMainOrchestratorState(
  events: NapcatEvent[],
  childTasks: ChildTaskItem[],
  traceStatus: string,
): MainOrchestratorState | null {
  const started = latestOf(events, "orchestrator.turn.started");
  const decision = latestOf(events, "orchestrator.decision.parsed");
  if (!started && !decision) {
    return null;
  }

  const reusedCount = childTasks.filter((child) => child.wasReused).length;
  const spawnCount = Number(decision?.payload?.spawn_count ?? 0) || 0;
  const cancelCount = Number(decision?.payload?.cancel_count ?? 0) || 0;
  const respondNow = Boolean(decision?.payload?.respond_now);
  const ignoreMessage = Boolean(decision?.payload?.ignore_message);
  const usedFallback = Boolean(decision?.payload?.used_fallback);
  const immediateReplyPreview = trimText(decision?.payload?.immediate_reply_preview || "", 180);
  const reasoningSummary = trimText(decision?.payload?.reasoning_summary || "", 180);
  const messagePreview = trimText(started?.payload?.message_preview || "", 180);
  const rawResponse = String(decision?.payload?.raw_response || "").trim();

  let status: MainOrchestratorState["status"] = "running";
  if (decision) {
    if (ignoreMessage) {
      status = "ignored";
    } else if (spawnCount > 0) {
      status = "spawned";
    } else if (reusedCount > 0) {
      status = "reused";
    } else if (respondNow) {
      status = "responded";
    }
  } else if (traceStatus === "error") {
    status = "failed";
  }

  let summary = "Main orchestrator is evaluating the turn.";
  if (status === "ignored") {
    summary = "Main orchestrator ignored the message.";
  } else if (status === "spawned") {
    summary = `Main orchestrator spawned ${spawnCount} child task${spawnCount === 1 ? "" : "s"}.`;
  } else if (status === "reused") {
    summary = `Main orchestrator reused ${reusedCount} active child task${reusedCount === 1 ? "" : "s"}.`;
  } else if (status === "responded") {
    summary = immediateReplyPreview
      ? `Main orchestrator replied directly: ${immediateReplyPreview}`
      : "Main orchestrator replied directly.";
  } else if (status === "failed") {
    summary = "Main orchestrator ended with an error.";
  }
  if (usedFallback) {
    summary = `${summary} Fallback heuristic was used.`;
  }
  if (reasoningSummary && status !== "responded") {
    summary = `${summary} ${reasoningSummary}`.trim();
  }

  return {
    status,
    startedAt: started?.ts ?? null,
    decidedAt: decision?.ts ?? null,
    usedFallback,
    respondNow,
    ignoreMessage,
    spawnCount,
    cancelCount,
    reusedCount,
    immediateReplyPreview,
    reasoningSummary,
    messagePreview,
    rawResponse,
    summary: trimText(summary, 240),
  };
}

function summarizeStage(key: TraceStageKey, events: NapcatEvent[], toolCalls: ToolCallItem[], streamChunks: StreamChunk[]): string {
  if (events.length === 0) {
    return "No events";
  }

  switch (key) {
    case "inbound": {
      const received = latestOf(events, "message.received");
      return trimText(
        received?.payload?.text_preview
        || received?.payload?.text
        || `Received ${events.length} inbound event${events.length === 1 ? "" : "s"}`,
        160,
      );
    }
    case "trigger": {
      const rejected = latestOf(events, "group.trigger.rejected");
      if (rejected) {
        return `Rejected: ${trimText(rejected.payload?.final_reason || rejected.payload?.rejection_layer || "blocked", 120)}`;
      }
      const evaluated = latestOf(events, "group.trigger.evaluated");
      if (evaluated) {
        return `Accepted: ${trimText(evaluated.payload?.trigger_reason || "triggered", 120)}`;
      }
      const contextOnly = latestOf(events, "group.context_only_emitted");
      if (contextOnly) {
        return `Context only: ${trimText(contextOnly.payload?.trigger_reason || "no reply", 120)}`;
      }
      return `${events.length} trigger event${events.length === 1 ? "" : "s"}`;
    }
    case "context": {
      const decision = latestOf(events, "orchestrator.decision.parsed");
      if (decision) {
        return trimText(
          decision.payload?.reasoning_summary
          || decision.payload?.immediate_reply_preview
          || "Orchestrator decision parsed",
          160,
        );
      }
      const started = latestOf(events, "agent.run.started");
      return trimText(
        started?.payload?.prompt
        ? `Prompt captured for ${String(started.payload?.model || "model")}`
        : started?.payload?.model || "Context prepared",
        160,
      );
    }
    case "model": {
      const lastRequest = latestOf(events, "agent.model.requested");
      if (streamChunks.length > 0) {
        const thinkingCount = streamChunks.filter((chunk) => chunk.kind === "thinking").length;
        const responseCount = streamChunks.filter((chunk) => chunk.kind === "response").length;
        if (thinkingCount > 0 && responseCount > 0) {
          return `Streaming ${thinkingCount} thinking + ${responseCount} response chunk${streamChunks.length === 1 ? "" : "s"}`;
        }
        if (thinkingCount > 0) {
          return `Streaming ${thinkingCount} thinking chunk${thinkingCount === 1 ? "" : "s"}`;
        }
        if (responseCount > 0) {
          return `Streaming ${responseCount} response chunk${responseCount === 1 ? "" : "s"}`;
        }
      }
      return trimText(lastRequest?.payload?.model || "Model activity", 160);
    }
    case "tools":
      return toolCalls.length > 0
        ? `${toolCalls.length} tool call${toolCalls.length === 1 ? "" : "s"}`
        : `${events.length} tool event${events.length === 1 ? "" : "s"}`;
    case "memory_skill_routing": {
      const memoryEvent = latestOf(events, "agent.memory.used");
      const skillEvent = latestOf(events, "agent.skill.used");
      return trimText(
        skillEvent?.payload?.skill_name
        || memoryEvent?.payload?.summary
        || "Memory / skill routing activity",
        160,
      );
    }
    case "final": {
      const turnIgnored = latestOf(events, "orchestrator.turn.ignored");
      const turnCompleted = latestOf(events, "orchestrator.turn.completed");
      const childCompleted = latestOf(events, "orchestrator.child.completed");
      const childCancelled = latestOf(events, "orchestrator.child.cancelled");
      const finalResponse = latestOf(events, "agent.response.final");
      const suppressed = latestOf(events, "agent.response.suppressed");
      const shortCircuit = latestOf(events, "gateway.turn.short_circuited");
      return trimText(
        turnIgnored?.payload?.reason
        || turnIgnored?.payload?.summary
        || turnCompleted?.payload?.summary
        || turnCompleted?.payload?.response_preview
        || childCompleted?.payload?.result_preview
        || childCompleted?.payload?.summary
        || childCancelled?.payload?.reason
        || childCancelled?.payload?.summary
        || finalResponse?.payload?.response_preview
        || shortCircuit?.payload?.response_preview
        || suppressed?.payload?.reason
        || "Finalized",
        160,
      );
    }
    case "error": {
      const childFailed = latestOf(events, "orchestrator.child.failed");
      const errorEvent = latestOf(events, "error.raised");
      return trimText(
        childFailed?.payload?.error
        || childFailed?.payload?.result_preview
        || childFailed?.payload?.summary
        || errorEvent?.payload?.error_message
        || errorEvent?.payload?.error
        || errorEvent?.payload?.traceback_summary
        || "Error raised",
        160,
      );
    }
    case "raw":
      return `${events.length} uncategorized event${events.length === 1 ? "" : "s"}`;
    default:
      return `${events.length} event${events.length === 1 ? "" : "s"}`;
  }
}

function stageStatusFor(
  key: TraceStageKey,
  events: NapcatEvent[],
  latestStage: TraceStageKey,
  traceStatus: string,
  activeStream: boolean,
): TraceStageStatus {
  if (events.length === 0) {
    return "idle";
  }
  if (key === "error" || traceStatus === "error") {
    return key === "error"
      || events.some((event) =>
        event.event_type === "error.raised" || event.event_type === "orchestrator.child.failed",
      )
      ? "error"
      : "completed";
  }
  if (latestStage === key && (activeStream || traceStatus === "in_progress")) {
    return "active";
  }
  return "completed";
}

function buildStages(
  groupedEvents: Map<TraceStageKey, NapcatEvent[]>,
  latestStage: TraceStageKey,
  traceStatus: string,
  activeStream: boolean,
  toolCalls: ToolCallItem[],
  streamChunks: StreamChunk[],
): TraceStage[] {
  return STAGE_ORDER
    .map((key) => {
      const events = groupedEvents.get(key) ?? [];
      return {
        key,
        title: STAGE_TITLES[key],
        status: stageStatusFor(key, events, latestStage, traceStatus, activeStream),
        summary: summarizeStage(key, events, toolCalls, streamChunks),
        events,
      };
    })
    .filter((stage) => stage.events.length > 0 || stage.key !== "raw");
}

function lanePriority(key: string): number {
  if (key === "main_orchestrator") {
    return 0;
  }
  if (key === "main_agent") {
    return 1;
  }
  if (key.startsWith("child:")) {
    return 2;
  }
  return 3;
}

function deriveLaneIdentity(
  event: NapcatEvent,
  options: {
    hasOrchestratorActivity: boolean;
    hasMainAgentLane: boolean;
  },
): Pick<AgentLane, "key" | "title" | "agentScope" | "childId"> {
  const agentScope = String(event.payload?.agent_scope || "").trim();
  const childId = String(event.payload?.child_id || "").trim();
  if (childId) {
    return {
      key: `child:${childId}`,
      title: `Child ${childId}`,
      agentScope: agentScope || "orchestrator_child",
      childId,
    };
  }
  if (
    agentScope === "main_orchestrator"
    || (
      event.event_type.startsWith("orchestrator.")
      && !event.event_type.startsWith("orchestrator.child.")
    )
  ) {
    return {
      key: "main_orchestrator",
      title: "Main Orchestrator",
      agentScope: "main_orchestrator",
      childId: "",
    };
  }
  if (event.event_type === "message.received" && options.hasOrchestratorActivity) {
    return {
      key: "main_orchestrator",
      title: "Main Orchestrator",
      agentScope: "main_orchestrator",
      childId: "",
    };
  }
  if (
    !agentScope
    && options.hasOrchestratorActivity
    && !options.hasMainAgentLane
    && [
      "agent.response.final",
      "agent.response.suppressed",
      "gateway.turn.short_circuited",
    ].includes(event.event_type)
  ) {
    return {
      key: "main_orchestrator",
      title: "Main Orchestrator",
      agentScope: "main_orchestrator",
      childId: "",
    };
  }
  return {
    key: "main_agent",
    title: "Main Agent",
    agentScope: agentScope || "main_agent",
    childId: "",
  };
}

function trimPayloadPreview(value: unknown, maxChars: number = 220): string {
  void maxChars;
  try {
    if (value == null) {
      return "";
    }
    return typeof value === "string" ? value.trim() : JSON.stringify(value, null, 2) ?? "";
  } catch {
    return String(value ?? "").trim();
  }
}

function zeroUsage(): LlmUsageSnapshot {
  return {
    inputTokens: 0,
    outputTokens: 0,
    cacheReadTokens: 0,
    cacheWriteTokens: 0,
    reasoningTokens: 0,
    promptTokens: 0,
    totalTokens: 0,
    apiCalls: 0,
  };
}

function parseUsage(event: NapcatEvent): LlmUsageSnapshot {
  return {
    inputTokens: Math.max(0, Number(event.payload?.input_tokens ?? 0) || 0),
    outputTokens: Math.max(0, Number(event.payload?.output_tokens ?? 0) || 0),
    cacheReadTokens: Math.max(0, Number(event.payload?.cache_read_tokens ?? 0) || 0),
    cacheWriteTokens: Math.max(0, Number(event.payload?.cache_write_tokens ?? 0) || 0),
    reasoningTokens: Math.max(0, Number(event.payload?.reasoning_tokens ?? 0) || 0),
    promptTokens: Math.max(0, Number(event.payload?.prompt_tokens ?? 0) || 0),
    totalTokens: Math.max(0, Number(event.payload?.total_tokens ?? 0) || 0),
    apiCalls: Math.max(0, Number(event.payload?.api_calls ?? 1) || 0),
  };
}

function normalizeComparableText(value: string): string {
  return value.replace(/\s+/g, " ").trim();
}

function applyThinkingDelta(block: MutableRequestBlock, event: NapcatEvent): void {
  const payload = event.payload ?? {};
  const content = typeof payload.content === "string" ? payload.content : "";
  const delta = typeof payload.delta === "string" ? payload.delta : "";
  const sequence = Number.isFinite(Number(payload.sequence))
    ? Number(payload.sequence)
    : block.thinkingSteps.length + 1;
  const currentFullText = block.thinkingFullText || block.thinkingText || "";
  let nextFullText = currentFullText;
  let nextSource: ThinkingStep["source"] = "delta";
  // Only treat content as a full replacement when it is at least as long as
  // the current accumulated text. This guards against truncated or malformed
  // content fields accidentally wiping earlier reasoning.
  if (content && content.length >= currentFullText.length) {
    if (!currentFullText || content.startsWith(currentFullText)) {
      nextFullText = content;
      nextSource = "suffix";
    } else if (normalizeComparableText(content) === normalizeComparableText(currentFullText)) {
      nextFullText = currentFullText;
      nextSource = "suffix";
    } else {
      nextFullText = content;
      nextSource = "reset";
    }
  } else if (delta.trim()) {
    nextFullText = `${currentFullText}${delta}`;
  }

  block.thinkingFullText = nextFullText;
  block.thinkingText = nextFullText;
  const comparable = normalizeComparableText(nextFullText);
  if (!comparable) {
    block.thinkingSteps = [];
    block.thinkingStepCount = 0;
    return;
  }
  block.thinkingSteps = [{
    sequence,
    ts: event.ts,
    text: nextFullText.trim(),
    source: nextSource,
  }];
  block.thinkingStepCount = 1;
}

function appendStreamText(existing: string, event: NapcatEvent): string {
  const fullContent = String(event.payload?.content || "").trim();
  if (fullContent) {
    return fullContent;
  }
  const delta = String(event.payload?.delta || "").trim();
  if (!delta) {
    return existing;
  }
  return `${existing}${delta}`;
}

function eventSummary(event: NapcatEvent): string {
  switch (event.event_type) {
    case "message.received":
      return trimText(
        String(
          event.payload?.text_preview
          || event.payload?.text
          || event.payload?.raw_message
          || "Inbound message",
        ),
        180,
      );
    case "orchestrator.turn.started":
      return trimText(String(event.payload?.message_preview || "Main orchestrator started evaluating the turn."), 180);
    case "orchestrator.decision.parsed":
      return trimText(
        String(
          event.payload?.reasoning_summary
          || event.payload?.immediate_reply_preview
          || "Main orchestrator parsed a routing decision.",
        ),
        180,
      );
    case "orchestrator.turn.ignored":
      return trimText(
        String(
          event.payload?.reason
          || event.payload?.summary
          || "Main orchestrator ignored the message.",
        ),
        180,
      );
    case "orchestrator.turn.completed":
      return trimText(
        String(
          event.payload?.summary
          || event.payload?.response_preview
          || "Main orchestrator finished the turn.",
        ),
        180,
      );
    case "orchestrator.child.spawned":
    case "orchestrator.child.reused":
    case "orchestrator.child.completed":
    case "orchestrator.child.cancelled":
    case "orchestrator.child.failed":
      return trimText(
        String(
          event.payload?.summary
          || event.payload?.result_preview
          || event.payload?.error
          || event.payload?.goal
          || event.payload?.reason
          || event.event_type,
        ),
        180,
      );
    case "agent.memory.called":
      return trimText(String(event.payload?.summary || event.payload?.source || "Memory prefetch started"), 180);
    case "agent.memory.used":
      return trimText(String(event.payload?.summary || event.payload?.source || "Memory activity"), 180);
    case "agent.skill.called":
      return trimText(String(event.payload?.summary || event.payload?.skill_name || "Skill call started"), 180);
    case "agent.skill.used":
      return trimText(String(event.payload?.summary || event.payload?.skill_name || "Skill activity"), 180);
    case "policy.applied":
      return trimText(String(event.payload?.summary || event.payload?.policy_name || "Policy applied"), 180);
    case "agent.tool.called":
      return trimText(String(event.payload?.tool_name || "Tool called"), 180);
    case "agent.tool.completed":
      return trimText(String(event.payload?.result_preview || event.payload?.tool_name || "Tool completed"), 180);
    case "agent.tool.failed":
      return trimText(String(event.payload?.error || event.payload?.tool_name || "Tool failed"), 180);
    case "agent.response.final":
      return trimText(String(event.payload?.response_preview || "Final response emitted"), 180);
    case "agent.response.suppressed":
      return trimText(String(event.payload?.reason || "Response suppressed"), 180);
    case "error.raised":
      return trimText(
        String(
          event.payload?.error_message
          || event.payload?.error
          || event.payload?.traceback_summary
          || "Error raised",
        ),
        180,
      );
    default:
      return trimText(String(event.payload?.summary || event.payload?.preview || event.event_type), 180);
  }
}

function timelineEntryKind(event: NapcatEvent): TimelineEntry["kind"] {
  if (event.event_type === "message.received") {
    return "inbound";
  }
  if (event.event_type === "agent.memory.called" || event.event_type === "agent.memory.used") {
    return "memory";
  }
  if (event.event_type === "agent.skill.called" || event.event_type === "agent.skill.used") {
    return "skill";
  }
  if (event.event_type === "policy.applied") {
    return "policy";
  }
  if (event.event_type === "agent.response.final" || event.event_type === "agent.response.suppressed") {
    return "final";
  }
  if (event.event_type === "error.raised" || event.event_type.endsWith(".failed")) {
    return "error";
  }
  if (event.event_type.startsWith("orchestrator.")) {
    return "orchestrator";
  }
  if (event.event_type.startsWith("agent.tool.")) {
    return "tool";
  }
  return "event";
}

function shouldRenderStandaloneTimelineEntry(event: NapcatEvent): boolean {
  return [
    "message.received",
    "orchestrator.turn.started",
    "orchestrator.decision.parsed",
    "orchestrator.turn.completed",
    "orchestrator.turn.ignored",
    "orchestrator.turn.failed",
    "orchestrator.child.spawned",
    "orchestrator.child.reused",
    "orchestrator.child.completed",
    "orchestrator.child.cancelled",
    "orchestrator.child.failed",
    "agent.tool.called",
    "agent.tool.completed",
    "agent.tool.failed",
    "agent.memory.called",
    "agent.memory.used",
    "agent.skill.called",
    "agent.skill.used",
    "policy.applied",
    "agent.response.final",
    "agent.response.suppressed",
    "gateway.turn.short_circuited",
    "error.raised",
  ].includes(event.event_type);
}

type MutableRequestBlock = LlmRequestBlock & {
  _order: number;
  _streaming: boolean | null;
};

function parseNonNegativeNumber(value: unknown): number | null {
  const numeric = Number(value);
  return Number.isFinite(numeric) && numeric >= 0 ? numeric : null;
}

function resolveDurationMs(payload: Record<string, unknown> | undefined): number | null {
  if (!payload) return null;
  const dur = parseNonNegativeNumber(payload.duration_ms);
  if (dur !== null) return dur;
  const timing = payload.timing as Record<string, unknown> | undefined;
  return parseNonNegativeNumber(timing?.duration_ms);
}

function deriveTtftMs(startedAt: number, eventTs: number): number {
  return Math.max(0, Math.round((eventTs - startedAt) * 1000));
}

function buildTraceTimeline(
  trace: NapcatTrace,
  events: NapcatEvent[],
  headline: string,
  status: string,
  lastEventAt: number,
): TraceTimeline {
  const lanes = new Map<string, AgentLane>();
  const requestBlocksByLane = new Map<string, Map<string, MutableRequestBlock>>();
  const laneOrder = new Map<string, number>();
  const openRequestIds = new Map<string, string>();
  const recentRequestIds = new Map<string, string>();
  let requestOrder = 0;

  // Open-operation tracking for lifecycle timeline entries
  const openToolCalls = new Map<string, Map<string, TimelineEntry>>();
  const openOrchestratorTurns = new Map<string, TimelineEntry>();
  const openChildTasks = new Map<string, TimelineEntry>();
  const openMemoryOps = new Map<string, TimelineEntry>();
  const openSkillOps = new Map<string, TimelineEntry>();

  const hasOrchestratorActivity = events.some((event) =>
    event.event_type.startsWith("orchestrator.")
    && !event.event_type.startsWith("orchestrator.child."),
  );

  const ensureLane = (event: NapcatEvent): AgentLane => {
    const identity = deriveLaneIdentity(event, {
      hasOrchestratorActivity,
      hasMainAgentLane: lanes.has("main_agent"),
    });
    const existing = lanes.get(identity.key);
    if (existing) {
      existing.agentScope = existing.agentScope || identity.agentScope;
      existing.childId = existing.childId || identity.childId;
      if (!existing.startedAt || event.ts < existing.startedAt) {
        existing.startedAt = event.ts;
      }
      if (!existing.endedAt || event.ts > existing.endedAt) {
        existing.endedAt = event.ts;
      }
      existing.rawEvents.push(event);
      return existing;
    }

    const lane: AgentLane = {
      key: identity.key,
      title: identity.title,
      agentScope: identity.agentScope,
      childId: identity.childId,
      model: "",
      provider: "",
      startedAt: event.ts,
      endedAt: event.ts,
      status: "idle",
      requestBlocks: [],
      timelineEntries: [],
      rawEvents: [event],
    };
    lanes.set(identity.key, lane);
    laneOrder.set(identity.key, laneOrder.size);
    return lane;
  };

  const ensureRequestBlock = (
    lane: AgentLane,
    requestId: string,
    event: NapcatEvent,
    correlationMode: "exact" | "legacy",
  ): MutableRequestBlock => {
    const byLane = requestBlocksByLane.get(lane.key) ?? new Map<string, MutableRequestBlock>();
    requestBlocksByLane.set(lane.key, byLane);
    const existing = byLane.get(requestId);
    if (existing) {
      existing.rawEvents.push(event);
      return existing;
    }

    const block: MutableRequestBlock = {
      id: requestId,
      correlationMode,
      laneKey: lane.key,
      agentScope: lane.agentScope,
      childId: lane.childId,
      model: "",
      provider: "",
      startedAt: event.ts,
      endedAt: null,
      ttftMs: null,
      durationMs: null,
      status: "running",
      requestBody: "",
      requestPreview: "",
      thinkingText: "",
      thinkingFullText: "",
      thinkingSteps: [],
      thinkingStepCount: 0,
      responseText: "",
      toolCalls: [],
      memoryEvents: [],
      skillEvents: [],
      policyEvents: [],
      rawEvents: [event],
      usage: zeroUsage(),
      _order: requestOrder++,
      _streaming: null,
    };
    byLane.set(requestId, block);
    return block;
  };

  const resolveRequestId = (lane: AgentLane, event: NapcatEvent): { requestId: string | null; correlationMode: "exact" | "legacy" } => {
    const explicit = String(event.payload?.llm_request_id || "").trim();
    if (explicit) {
      if (event.event_type === "agent.model.requested") {
        openRequestIds.set(lane.key, explicit);
      }
      recentRequestIds.set(lane.key, explicit);
      return { requestId: explicit, correlationMode: "exact" };
    }
    if (event.event_type === "agent.model.requested") {
      const synthetic = `legacy:${lane.key}:requested:${event.event_id}`;
      openRequestIds.set(lane.key, synthetic);
      recentRequestIds.set(lane.key, synthetic);
      return { requestId: synthetic, correlationMode: "legacy" };
    }
    const currentOpen = openRequestIds.get(lane.key) || recentRequestIds.get(lane.key) || "";
    if (currentOpen) {
      return { requestId: currentOpen, correlationMode: currentOpen.startsWith("legacy:") ? "legacy" : "exact" };
    }
    if (
      event.event_type === "agent.tool.called"
      || event.event_type === "agent.tool.completed"
      || event.event_type === "agent.tool.failed"
      || event.event_type === "agent.reasoning.delta"
      || event.event_type === "agent.response.delta"
      || event.event_type === "agent.model.completed"
    ) {
      const synthetic = `legacy:${lane.key}:implicit:${event.event_id}`;
      recentRequestIds.set(lane.key, synthetic);
      return { requestId: synthetic, correlationMode: "legacy" };
    }
    return { requestId: null, correlationMode: "legacy" };
  };

  for (const event of events) {
    const lane = ensureLane(event);
    lane.status = lane.status === "error" || event.event_type.endsWith(".failed")
      ? "error"
      : status === "completed" && lane.status !== "error"
        ? "completed"
        : "running";

    const { requestId, correlationMode } = resolveRequestId(lane, event);
    if (requestId) {
      const block = ensureRequestBlock(lane, requestId, event, correlationMode);
      block.agentScope = block.agentScope || lane.agentScope;
      block.childId = block.childId || lane.childId;
      block.model = block.model || String(event.payload?.model || "");
      block.provider = block.provider || String(event.payload?.provider || "");
      block.startedAt = Math.min(block.startedAt, event.ts);
      if (!lane.model && block.model) {
        lane.model = block.model;
      }
      if (!lane.provider && block.provider) {
        lane.provider = block.provider;
      }

      switch (event.event_type) {
        case "agent.model.requested":
          block.requestBody = block.requestBody || String(event.payload?.request_body || "");
          block.requestPreview = block.requestPreview || trimPayloadPreview(event.payload?.request_preview || event.payload?.request_body || "");
          if (typeof event.payload?.streaming === "boolean") {
            block._streaming = event.payload.streaming;
          }
          block.status = "running";
          openRequestIds.set(lane.key, requestId);
          recentRequestIds.set(lane.key, requestId);
          break;
        case "agent.model.completed":
          block.endedAt = event.ts;
          block.durationMs = resolveDurationMs(event.payload) ?? Math.max(0, (event.ts - block.startedAt) * 1000);
          block.ttftMs = parseNonNegativeNumber(event.payload?.ttft_ms)
            ?? parseNonNegativeNumber(event.payload?.time_to_first_token_ms)
            ?? block.ttftMs
            ?? (block._streaming === false ? block.durationMs : null);
          block.status = event.payload?.success === false ? "failed" : "completed";
          block.usage = parseUsage(event);
          if (openRequestIds.get(lane.key) === requestId) {
            openRequestIds.delete(lane.key);
          }
          break;
        case "agent.reasoning.delta":
          block.ttftMs = block.ttftMs ?? deriveTtftMs(block.startedAt, event.ts);
          applyThinkingDelta(block, event);
          break;
        case "agent.response.delta":
          block.ttftMs = block.ttftMs ?? deriveTtftMs(block.startedAt, event.ts);
          block.responseText = appendStreamText(block.responseText, event);
          break;
        case "agent.memory.used":
          block.memoryEvents.push(event);
          break;
        case "agent.skill.used":
          block.skillEvents.push(event);
          break;
        case "policy.applied":
          block.policyEvents.push(event);
          break;
        case "agent.response.final":
          if (!block.responseText) {
            block.responseText = String(event.payload?.response_preview || "");
          }
          break;
        default:
          break;
      }
    }

    const requestLocalOnly = Boolean(
      requestId
      && ["agent.memory.used", "agent.skill.used", "policy.applied"].includes(event.event_type),
    );

    const makeTimelineEntry = (evt: NapcatEvent, reqId: string | null, st: string): TimelineEntry => ({
      id: evt.event_id,
      kind: timelineEntryKind(evt),
      ts: evt.ts,
      startedAt: evt.ts,
      endedAt: st !== "running" ? evt.ts : null,
      laneKey: lane.key,
      llmRequestId: reqId,
      title: evt.event_type,
      summary: eventSummary(evt),
      status: st,
      durationMs: resolveDurationMs(evt.payload),
      payloadPreview: trimPayloadPreview(evt.payload),
      event: evt,
    });

    if (shouldRenderStandaloneTimelineEntry(event) && !requestLocalOnly) {
      const payload = (event.payload ?? {}) as Record<string, unknown>;

      // Tool call lifecycle
      if (event.event_type === "agent.tool.called") {
        const toolCallId = String(payload.tool_call_id || "");
        const entry = makeTimelineEntry(event, requestId, "running");
        const byLane = openToolCalls.get(lane.key) ?? new Map<string, TimelineEntry>();
        byLane.set(toolCallId, entry);
        openToolCalls.set(lane.key, byLane);
      } else if (event.event_type === "agent.tool.completed") {
        const toolCallId = String(payload.tool_call_id || "");
        const byLane = openToolCalls.get(lane.key);
        const openEntry = byLane?.get(toolCallId);
        if (byLane && openEntry) {
          byLane.delete(toolCallId);
          openEntry.status = "completed";
          openEntry.endedAt = event.ts;
          const dur = resolveDurationMs(payload);
          openEntry.durationMs = dur !== null
            ? dur
            : Math.max(0, (event.ts - openEntry.startedAt) * 1000);
          openEntry.title = event.event_type;
          openEntry.summary = eventSummary(event);
          lane.timelineEntries.push(openEntry);
        } else {
          lane.timelineEntries.push(makeTimelineEntry(event, requestId, "completed"));
        }
      } else if (event.event_type === "agent.tool.failed") {
        const toolCallId = String(payload.tool_call_id || "");
        const byLane = openToolCalls.get(lane.key);
        const openEntry = byLane?.get(toolCallId);
        if (byLane && openEntry) {
          byLane.delete(toolCallId);
          openEntry.status = "failed";
          openEntry.endedAt = event.ts;
          const dur = resolveDurationMs(payload);
          openEntry.durationMs = dur !== null
            ? dur
            : Math.max(0, (event.ts - openEntry.startedAt) * 1000);
          openEntry.title = event.event_type;
          openEntry.summary = eventSummary(event);
          lane.timelineEntries.push(openEntry);
        } else {
          lane.timelineEntries.push(makeTimelineEntry(event, requestId, "failed"));
        }

      // Orchestrator turn lifecycle
      } else if (event.event_type === "orchestrator.turn.started") {
        const entry = makeTimelineEntry(event, requestId, "running");
        const prevEntry = openOrchestratorTurns.get(lane.key);
        if (prevEntry) {
          lane.timelineEntries.push(prevEntry);
        }
        openOrchestratorTurns.set(lane.key, entry);
      } else if (
        event.event_type === "orchestrator.turn.completed"
        || event.event_type === "orchestrator.turn.ignored"
        || event.event_type === "orchestrator.turn.failed"
      ) {
        const openEntry = openOrchestratorTurns.get(lane.key);
        if (openEntry) {
          openOrchestratorTurns.delete(lane.key);
          const statusMap: Record<string, string> = {
            "orchestrator.turn.completed": "completed",
            "orchestrator.turn.ignored": "ignored",
            "orchestrator.turn.failed": "failed",
          };
          openEntry.status = statusMap[event.event_type];
          openEntry.endedAt = event.ts;
          const dur = resolveDurationMs(payload);
          openEntry.durationMs = dur !== null
            ? dur
            : Math.max(0, (event.ts - openEntry.startedAt) * 1000);
          openEntry.title = event.event_type;
          openEntry.summary = eventSummary(event);
          lane.timelineEntries.push(openEntry);
        } else {
          const statusMap: Record<string, string> = {
            "orchestrator.turn.completed": "completed",
            "orchestrator.turn.ignored": "ignored",
            "orchestrator.turn.failed": "failed",
          };
          lane.timelineEntries.push(makeTimelineEntry(event, requestId, statusMap[event.event_type]));
        }

      // Child task lifecycle
      } else if (event.event_type === "orchestrator.child.spawned") {
        const childId = String(payload.child_id || "");
        if (childId) {
          const entry = makeTimelineEntry(event, requestId, "running");
          openChildTasks.set(childId, entry);
        } else {
          lane.timelineEntries.push(makeTimelineEntry(event, requestId, "completed"));
        }
      } else if (
        event.event_type === "orchestrator.child.completed"
        || event.event_type === "orchestrator.child.cancelled"
        || event.event_type === "orchestrator.child.failed"
      ) {
        const childId = String(payload.child_id || "");
        const openEntry = childId ? openChildTasks.get(childId) : undefined;
        if (openEntry) {
          openChildTasks.delete(childId);
          const statusMap: Record<string, string> = {
            "orchestrator.child.completed": "completed",
            "orchestrator.child.cancelled": "cancelled",
            "orchestrator.child.failed": "failed",
          };
          openEntry.status = statusMap[event.event_type];
          openEntry.endedAt = event.ts;
          const dur = resolveDurationMs(payload);
          openEntry.durationMs = dur !== null
            ? dur
            : Math.max(0, (event.ts - openEntry.startedAt) * 1000);
          openEntry.title = event.event_type;
          openEntry.summary = eventSummary(event);
          lane.timelineEntries.push(openEntry);
        } else {
          const statusMap: Record<string, string> = {
            "orchestrator.child.completed": "completed",
            "orchestrator.child.cancelled": "cancelled",
            "orchestrator.child.failed": "failed",
          };
          lane.timelineEntries.push(makeTimelineEntry(event, requestId, statusMap[event.event_type]));
        }

      // Memory lifecycle
      } else if (event.event_type === "agent.memory.called") {
        const entry = makeTimelineEntry(event, requestId, "running");
        openMemoryOps.set(lane.key, entry);
      } else if (event.event_type === "agent.memory.used") {
        const openEntry = openMemoryOps.get(lane.key);
        if (openEntry) {
          openMemoryOps.delete(lane.key);
          openEntry.status = "completed";
          openEntry.endedAt = event.ts;
          const dur = resolveDurationMs(payload);
          openEntry.durationMs = dur !== null
            ? dur
            : Math.max(0, (event.ts - openEntry.startedAt) * 1000);
          openEntry.title = event.event_type;
          openEntry.summary = eventSummary(event);
          lane.timelineEntries.push(openEntry);
        } else {
          lane.timelineEntries.push(makeTimelineEntry(event, requestId, "completed"));
        }

      // Skill lifecycle
      } else if (event.event_type === "agent.skill.called") {
        const entry = makeTimelineEntry(event, requestId, "running");
        openSkillOps.set(lane.key, entry);
      } else if (event.event_type === "agent.skill.used") {
        const openEntry = openSkillOps.get(lane.key);
        if (openEntry) {
          openSkillOps.delete(lane.key);
          openEntry.status = "completed";
          openEntry.endedAt = event.ts;
          const dur = resolveDurationMs(payload);
          openEntry.durationMs = dur !== null
            ? dur
            : Math.max(0, (event.ts - openEntry.startedAt) * 1000);
          openEntry.title = event.event_type;
          openEntry.summary = eventSummary(event);
          lane.timelineEntries.push(openEntry);
        } else {
          lane.timelineEntries.push(makeTimelineEntry(event, requestId, "completed"));
        }

      // All other standalone events (single-shot, no lifecycle)
      } else {
        lane.timelineEntries.push(makeTimelineEntry(event, requestId,
          event.event_type.endsWith(".failed") || event.event_type === "error.raised" ? "error" : "completed"));
      }
    }
  }

  // Flush remaining open entries
  for (const [laneKey, entry] of openOrchestratorTurns) {
    const l = lanes.get(laneKey);
    if (l) {
      if (status === "completed" && entry.status === "running") {
        entry.status = "completed";
        entry.endedAt = entry.startedAt;
      }
      l.timelineEntries.push(entry);
    }
  }
  for (const [laneKey, tools] of openToolCalls) {
    const l = lanes.get(laneKey);
    if (l) {
      for (const entry of tools.values()) {
        l.timelineEntries.push(entry);
      }
    }
  }
  for (const [, entry] of openChildTasks) {
    const l = lanes.get(entry.laneKey);
    if (l) {
      l.timelineEntries.push(entry);
    }
  }
  for (const [laneKey, entry] of openMemoryOps) {
    const l = lanes.get(laneKey);
    if (l) {
      if (status === "completed" && entry.status === "running") {
        entry.status = "completed";
        entry.endedAt = entry.startedAt;
      }
      l.timelineEntries.push(entry);
    }
  }
  for (const [laneKey, entry] of openSkillOps) {
    const l = lanes.get(laneKey);
    if (l) {
      if (status === "completed" && entry.status === "running") {
        entry.status = "completed";
        entry.endedAt = entry.startedAt;
      }
      l.timelineEntries.push(entry);
    }
  }

  const laneList = [...lanes.values()]
    .map((lane) => {
      const blocks = [...(requestBlocksByLane.get(lane.key)?.values() ?? [])]
        .sort((left, right) => {
          if (left.startedAt !== right.startedAt) {
            return left.startedAt - right.startedAt;
          }
          return left._order - right._order;
        })
        .map(({ _order, _streaming, ...block }) => ({
          ...block,
          toolCalls: buildToolCalls(block.rawEvents),
        }));
      const requestEntries: TimelineEntry[] = blocks.map((block) => ({
        id: `${lane.key}:${block.id}`,
        kind: "llm_request",
        ts: block.startedAt,
        startedAt: block.startedAt,
        endedAt: block.endedAt,
        laneKey: lane.key,
        llmRequestId: block.id,
        title: block.model || "LLM request",
        summary: trimText(
          block.responseText
          || block.thinkingText
          || block.requestPreview
          || block.requestBody
          || "Model invocation",
          180,
        ),
        status: block.status,
        durationMs: block.durationMs,
        payloadPreview: trimPayloadPreview({
          requestBody: block.requestBody,
          thinking: block.thinkingText,
          response: block.responseText,
        }),
        event: block.rawEvents[0] ?? null,
      }));
      const timelineEntries = [...lane.timelineEntries, ...requestEntries].sort((left, right) => {
        if (left.ts !== right.ts) {
          return left.ts - right.ts;
        }
        return left.id.localeCompare(right.id);
      });
      const lastEvent = lane.rawEvents[lane.rawEvents.length - 1];
      const isIgnoredLane = lane.rawEvents.some((event) => event.event_type === "orchestrator.turn.ignored");
      return {
        ...lane,
        status: lane.rawEvents.some((event) => event.event_type.endsWith(".failed") || event.event_type === "error.raised")
          ? "error"
          : isIgnoredLane
            ? "ignored"
          : lane.rawEvents.some((event) => event.event_type === "agent.model.requested")
            && !lane.rawEvents.some((event) => event.event_type === "agent.model.completed")
            ? "running"
            : "completed",
        endedAt: lastEvent?.ts ?? lane.endedAt,
        requestBlocks: blocks,
        timelineEntries,
      };
    })
    .filter((lane) => lane.requestBlocks.length > 0 || lane.timelineEntries.length > 0)
    .sort((left, right) => {
      const priorityDelta = lanePriority(left.key) - lanePriority(right.key);
      if (priorityDelta !== 0) {
        return priorityDelta;
      }
      if ((left.startedAt ?? 0) !== (right.startedAt ?? 0)) {
        return (left.startedAt ?? 0) - (right.startedAt ?? 0);
      }
      return left.key.localeCompare(right.key);
    });

  return {
    traceId: trace.trace_id,
    sessionKey: trace.session_key,
    headline,
    status,
    lastEventAt,
    requestCount: laneList.reduce((sum, lane) => sum + lane.requestBlocks.length, 0),
    laneCount: laneList.length,
    lanes: laneList,
  };
}

function deriveHeadline(
  trace: NapcatTrace,
  events: NapcatEvent[],
  childTasks: ChildTaskItem[],
): Pick<LiveTraceGroup, "headline" | "headlineSource"> {
  const childError = childTasks.find((child) => child.status === "failed" && child.summary);
  if (childError) {
    return {
      headline: childError.summary,
      headlineSource: "child_error",
    };
  }

  const childResult = childTasks.find((child) => child.status === "completed" && child.summary);
  if (childResult) {
    return {
      headline: childResult.summary,
      headlineSource: "child_result",
    };
  }

  const finalResponse = latestOf(events, "agent.response.final");
  if (finalResponse?.payload?.response_preview) {
    return {
      headline: String(finalResponse.payload.response_preview),
      headlineSource: "response",
    };
  }

  const errorEvent = latestOf(events, "error.raised");
  if (errorEvent) {
    return {
      headline: String(
        errorEvent.payload?.error_message
        || errorEvent.payload?.error
        || errorEvent.payload?.traceback_summary
        || "Error raised",
      ),
      headlineSource: "error",
    };
  }

  const inbound = latestOf(events, "message.received");

  const contextOnly = latestOf(events, "group.context_only_emitted");
  if (contextOnly) {
    const coReason = String(
      contextOnly.payload?.trigger_reason
      || contextOnly.payload?.reason
      || "no reply",
    ).trim();
    const coPreview = String(
      inbound?.payload?.text_preview
      || inbound?.payload?.text
      || "[image attachment]",
    ).trim();
    return {
      headline: `Context only: ${coPreview} (${coReason})`,
      headlineSource: "context_only",
    };
  }

  if (inbound) {
    return {
      headline: String(
        inbound.payload?.text_preview
        || inbound.payload?.text
        || inbound.payload?.raw_message
        || "Inbound message",
      ),
      headlineSource: "inbound",
    };
  }

  const summaryHeadline = String(trace.summary?.headline || "").trim();
  if (summaryHeadline) {
    return {
      headline: summaryHeadline,
      headlineSource: "summary",
    };
  }

  const traceCreated = latestOf(events, "trace.created");
  if (traceCreated && (trace.chat_id || trace.message_id || trace.user_id)) {
    const chatType = String(trace.chat_type || traceCreated.chat_type || "").trim().toLowerCase();
    return {
      headline: chatType === "group" ? "Inbound group message received" : "Inbound message received",
      headlineSource: "trace",
    };
  }

  return {
    headline: "No trace headline available",
    headlineSource: "empty",
  };
}

export function mapTraceToLiveGroup(trace: NapcatTrace, rawEvents: NapcatEvent[]): LiveTraceGroup {
  const events = sortTraceEvents(rawEvents);
  const groupedEvents = new Map<TraceStageKey, NapcatEvent[]>();
  for (const stage of STAGE_ORDER) {
    groupedEvents.set(stage, []);
  }

  for (const event of events) {
    const stage = fallbackStageForEvent(event);
    groupedEvents.get(stage)?.push(event);
  }

  const streamChunks = buildStreamChunks(events);
  const toolCalls = buildToolCalls(events);
  const childTasks = buildChildTasks(events);
  const childDetailsById = buildChildDetails(events, childTasks);
  const activeChildren = childTasks.filter((child) => child.isActive);
  const recentChildren = childTasks.filter((child) => !child.isActive);
  const mainOrchestrator = buildMainOrchestratorState(events, childTasks, trace.status);
  const derivedActiveStream = deriveActiveStream(events);
  const activeStream = derivedActiveStream ?? Boolean(trace.active_stream);

  const summaryStage = String(trace.latest_stage || "").trim();
  let latestStage: TraceStageKey = STAGE_ORDER.includes(summaryStage as TraceStageKey)
    ? summaryStage as TraceStageKey
    : "raw";
  if (latestStage === "raw" && summaryStage !== "raw") {
    latestStage = "raw";
  }
  if (latestStage === "raw" && events.length > 0) {
    for (const event of events) {
      const stage = fallbackStageForEvent(event);
      if (STAGE_PRIORITY[stage] >= STAGE_PRIORITY[latestStage]) {
        latestStage = stage;
      }
    }
  }

  const { headline, headlineSource } = deriveHeadline(trace, events, childTasks);
  const lastEventAt = events[events.length - 1]?.ts ?? trace.ended_at ?? trace.started_at;
  const endedAt = trace.ended_at ?? (
    trace.status === "in_progress" ? null : lastEventAt
  );
  const timeline = buildTraceTimeline(trace, events, headline, trace.status, lastEventAt);

  return {
    traceId: trace.trace_id,
    status: trace.status,
    startedAt: trace.started_at,
    endedAt,
    durationMs: endedAt != null ? (endedAt - trace.started_at) * 1000 : null,
    latestStage,
    activeStream,
    eventCount: trace.event_count || events.length,
    toolCallCount: trace.tool_call_count ?? toolCalls.length,
    errorCount: trace.error_count ?? events.filter((event) =>
      event.event_type === "error.raised" || event.event_type === "orchestrator.child.failed",
    ).length,
    model: String(trace.model || trace.summary?.model || events.find((event) => event.payload?.model)?.payload?.model || ""),
    headline,
    headlineSource,
    lastEventAt,
    mainOrchestrator,
    childTasks,
    childDetailsById,
    activeChildren,
    recentChildren,
    stages: buildStages(groupedEvents, latestStage, trace.status, activeStream, toolCalls, streamChunks),
    toolCalls,
    streamChunks,
    rawEvents: events,
    timeline,
    trace,
  };
}
