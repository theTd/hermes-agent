import type {
  DashboardStats,
  NapcatAlert,
  NapcatEvent,
  NapcatSession,
  NapcatTrace,
  PublisherStats,
  RuntimeState,
  TraceFilters,
  WsBackfillCompleteMessage,
  WsIncomingMessage,
} from "@/lib/napcat-api";

export type ObservabilityView =
  | "all"
  | "active"
  | "errors"
  | "rejected"
  | "tools"
  | "streaming"
  | "alerts";

export type TraceStageKey =
  | "inbound"
  | "trigger"
  | "context"
  | "model"
  | "tools"
  | "memory_skill_routing"
  | "final"
  | "error"
  | "raw";

export type TraceStageStatus = "idle" | "active" | "completed" | "error";
export type ToolCallStatus = "running" | "completed" | "failed";
export type StreamChunkKind = "thinking" | "response";
export type ThinkingStepSource = "delta" | "suffix" | "reset";
export type OrchestratorMainStatus =
  | "idle"
  | "running"
  | "responded"
  | "spawned"
  | "ignored"
  | "reused"
  | "failed";
export type ChildTaskStatus =
  | "queued"
  | "running"
  | "reused"
  | "completed"
  | "failed"
  | "cancelled";

export interface StreamChunk {
  kind: StreamChunkKind;
  content: string;
  ts: number;
  sequence: number;
  stage: TraceStageKey;
  source: string;
  agentScope: string;
  childId: string;
}

export interface ToolCallItem {
  id: string;
  toolName: string;
  status: ToolCallStatus;
  startedAt: number;
  endedAt: number | null;
  durationMs: number | null;
  argsPreview: string;
  resultPreview: string;
  stdoutPreview: string;
  stderrPreview: string;
  error: string;
}

export interface LlmUsageSnapshot {
  inputTokens: number;
  outputTokens: number;
  cacheReadTokens: number;
  cacheWriteTokens: number;
  reasoningTokens: number;
  promptTokens: number;
  totalTokens: number;
  apiCalls: number;
}

export interface ThinkingStep {
  sequence: number;
  ts: number;
  text: string;
  source: ThinkingStepSource;
}

export interface TimelineEntry {
  id: string;
  kind:
    | "inbound"
    | "orchestrator"
    | "llm_request"
    | "memory"
    | "skill"
    | "policy"
    | "tool"
    | "final"
    | "error"
    | "event";
  ts: number;
  startedAt: number;
  endedAt: number | null;
  laneKey: string;
  llmRequestId: string | null;
  title: string;
  summary: string;
  status: string;
  durationMs: number | null;
  payloadPreview: string;
  event: NapcatEvent | null;
}

export interface LlmRequestBlock {
  id: string;
  correlationMode: "exact" | "legacy";
  laneKey: string;
  agentScope: string;
  childId: string;
  model: string;
  provider: string;
  startedAt: number;
  endedAt: number | null;
  ttftMs: number | null;
  durationMs: number | null;
  status: "running" | "completed" | "failed";
  requestBody: string;
  requestPreview: string;
  thinkingText: string;
  thinkingFullText: string;
  thinkingSteps: ThinkingStep[];
  thinkingStepCount: number;
  responseText: string;
  toolCalls: ToolCallItem[];
  memoryEvents: NapcatEvent[];
  skillEvents: NapcatEvent[];
  policyEvents: NapcatEvent[];
  rawEvents: NapcatEvent[];
  usage: LlmUsageSnapshot;
}

export interface AgentLane {
  key: string;
  title: string;
  agentScope: string;
  childId: string;
  model: string;
  provider: string;
  startedAt: number | null;
  endedAt: number | null;
  status: string;
  requestBlocks: LlmRequestBlock[];
  timelineEntries: TimelineEntry[];
  rawEvents: NapcatEvent[];
}

export interface TraceTimeline {
  traceId: string;
  sessionKey: string;
  headline: string;
  status: string;
  lastEventAt: number;
  requestCount: number;
  laneCount: number;
  lanes: AgentLane[];
}

export interface SessionTimelineGroup {
  sessionKey: string;
  session: NapcatSession | null;
  traces: LiveTraceGroup[];
  lastSeen: number;
  activeCount: number;
  errorCount: number;
}

export interface TraceStage {
  key: TraceStageKey;
  title: string;
  status: TraceStageStatus;
  summary: string;
  events: NapcatEvent[];
}

export interface MainOrchestratorState {
  status: OrchestratorMainStatus;
  startedAt: number | null;
  decidedAt: number | null;
  usedFallback: boolean;
  respondNow: boolean;
  ignoreMessage: boolean;
  spawnCount: number;
  cancelCount: number;
  reusedCount: number;
  immediateReplyPreview: string;
  reasoningSummary: string;
  messagePreview: string;
  rawResponse: string;
  summary: string;
}

export interface ChildTaskItem {
  childId: string;
  goal: string;
  requestKind: string;
  runtimeSessionId: string;
  runtimeSessionKey: string;
  status: ChildTaskStatus;
  startedAt: number | null;
  endedAt: number | null;
  durationMs: number | null;
  resultPreview: string;
  error: string;
  reason: string;
  summary: string;
  lastEventAt: number;
  eventCount: number;
  isActive: boolean;
  wasReused: boolean;
}

export interface ChildTraceDetail {
  childId: string;
  task: ChildTaskItem;
  status: string;
  startedAt: number | null;
  endedAt: number | null;
  durationMs: number | null;
  latestStage: TraceStageKey;
  activeStream: boolean;
  eventCount: number;
  toolCallCount: number;
  errorCount: number;
  model: string;
  lastEventAt: number;
  stages: TraceStage[];
  toolCalls: ToolCallItem[];
  streamChunks: StreamChunk[];
  rawEvents: NapcatEvent[];
}

export interface LiveTraceGroup {
  traceId: string;
  status: string;
  startedAt: number;
  endedAt: number | null;
  durationMs: number | null;
  latestStage: TraceStageKey;
  activeStream: boolean;
  eventCount: number;
  toolCallCount: number;
  errorCount: number;
  model: string;
  headline: string;
  headlineSource: "response" | "error" | "child_result" | "child_error" | "inbound" | "summary" | "trace" | "empty";
  lastEventAt: number;
  mainOrchestrator: MainOrchestratorState | null;
  childTasks: ChildTaskItem[];
  childDetailsById: Record<string, ChildTraceDetail>;
  activeChildren: ChildTaskItem[];
  recentChildren: ChildTaskItem[];
  stages: TraceStage[];
  toolCalls: ToolCallItem[];
  streamChunks: StreamChunk[];
  rawEvents: NapcatEvent[];
  timeline: TraceTimeline;
  trace: NapcatTrace;
}

export interface ConnectionState {
  wsConnected: boolean;
  paused: boolean;
  followLatest: boolean;
  reconnecting: boolean;
  latestCursor: number | null;
  unreadWhilePaused: number;
}

export interface FilterState {
  status: TraceFilters["status"] | "";
  eventType: string;
  search: string;
  sessionKey: string;
  chatId: string;
  userId: string;
  view: ObservabilityView;
}

export interface EntityState {
  tracesById: Record<string, NapcatTrace>;
  eventsByTraceId: Record<string, NapcatEvent[]>;
  alertsById: Record<string, NapcatAlert>;
  sessionsByKey: Record<string, NapcatSession>;
  groupsById: Record<string, LiveTraceGroup>;
}

export interface UiState {
  selectedTraceId: string | null;
  expandedTraceIds: Record<string, boolean>;
  expandedRawEventIds: Record<string, boolean>;
  activeInspectorSection: string | null;
}

export interface ObservabilityState {
  connection: ConnectionState;
  filters: FilterState;
  entities: EntityState;
  runtime: RuntimeState;
  stats: PublisherStats | null;
  dashboardStats: DashboardStats | null;
  ui: UiState;
  bootstrapped: boolean;
  error: string | null;
}

export interface BootstrapSnapshot {
  traces?: NapcatTrace[];
  events?: NapcatEvent[];
  sessions?: NapcatSession[];
  alerts?: NapcatAlert[];
  runtime?: RuntimeState;
  stats?: PublisherStats | null;
  cursor?: number | null;
  replaceEntities?: boolean;
  groups?: LiveTraceGroup[];
  dashboardStats?: DashboardStats | null;
}

export type ObservabilityAction =
  | { type: "bootstrap_from_snapshot"; payload: BootstrapSnapshot }
  | { type: "apply_trace_update"; payload: { trace: NapcatTrace } }
  | { type: "apply_group_update"; payload: { group: LiveTraceGroup } }
  | { type: "append_event"; payload: { event: NapcatEvent } }
  | { type: "append_alert"; payload: { alert: NapcatAlert } }
  | { type: "replace_runtime"; payload: { runtime: RuntimeState } }
  | { type: "apply_dashboard_update"; payload: { dashboardStats: DashboardStats } }
  | { type: "set_filters"; payload: Partial<FilterState> }
  | { type: "set_selected_trace"; payload: { traceId: string | null } }
  | { type: "toggle_trace_expanded"; payload: { traceId: string } }
  | { type: "toggle_raw_event_expanded"; payload: { eventId: string } }
  | { type: "toggle_follow_latest"; payload?: { value?: boolean } }
  | { type: "pause_stream" }
  | { type: "resume_stream" }
  | { type: "mark_backfill_complete"; payload: WsBackfillCompleteMessage["data"] }
  | { type: "set_active_inspector_section"; payload: { section: string | null } }
  | { type: "set_connection_state"; payload: Partial<ConnectionState> }
  | { type: "record_buffered_message"; payload: { cursor?: number | null; count?: number } }
  | { type: "clear_observability_data" }
  | { type: "set_error"; payload: { error: string | null } };

export interface ObservabilityController {
  state: ObservabilityState;
  traces: NapcatTrace[];
  groups: LiveTraceGroup[];
  sessions: NapcatSession[];
  alerts: NapcatAlert[];
  selectedTrace: NapcatTrace | null;
  selectedTraceEvents: NapcatEvent[];
  selectedGroup: LiveTraceGroup | null;
  eventRate10s: number;
  dashboardStats: DashboardStats | null;
  setFilters: (patch: Partial<FilterState>) => void;
  setSelectedTrace: (traceId: string | null) => void;
  toggleTraceExpanded: (traceId: string) => void;
  toggleRawEventExpanded: (eventId: string) => void;
  toggleFollowLatest: (value?: boolean) => void;
  pauseStream: () => void;
  resumeStream: () => void;
  setActiveInspectorSection: (section: string | null) => void;
  reconnect: () => void;
  clearFilters: () => void;
  clearAllData: () => Promise<void>;
}

export function isBackfillCompleteMessage(
  message: WsIncomingMessage,
): message is WsBackfillCompleteMessage {
  return message.type === "backfill.complete";
}

export function createDefaultFilters(): FilterState {
  return {
    status: "",
    eventType: "",
    search: "",
    sessionKey: "",
    chatId: "",
    userId: "",
    view: "all",
  };
}
